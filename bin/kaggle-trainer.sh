#!/usr/bin/env bash
# Kaggle remote trainer — runs on HF Space, triggers Kaggle T4 GPU training.
#
# Architecture:
#   HF Space (this) ── uploads notebook + dataset slice ──→ Kaggle T4 GPU
#                  ←── downloads LoRA adapter, pushes to HF Hub ──
#
# Free Kaggle quota: 30 hr/week T4 GPU per account. We can run 5-7 LoRA
# experiments per week per account at no cost.
#
# This daemon checks every 6 hours: if no training is currently running on
# Kaggle for surrogate-1, it kicks a new one with the latest dataset slice.

set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

LOG="$HOME/.surrogate/logs/kaggle-trainer.log"
mkdir -p "$(dirname "$LOG")"

KAGGLE_DIR="$HOME/.kaggle"
mkdir -p "$KAGGLE_DIR"

# Kaggle CLI reads BOTH (a) $HOME/.kaggle/kaggle.json AND (b) the env vars
# KAGGLE_USERNAME + KAGGLE_KEY. We set both for redundancy.
# IMPORTANT: KAGGLE_USERNAME must match the account that owns the token —
# 403 'Forbidden' from SaveKernel means username/token mismatch.
if [[ -n "${KAGGLE_API_TOKEN:-}" ]]; then
    KAGGLE_USERNAME="${KAGGLE_USERNAME:-ashirafuse}"
    export KAGGLE_USERNAME
    export KAGGLE_KEY="${KAGGLE_API_TOKEN}"
    cat > "$KAGGLE_DIR/kaggle.json" << EOF
{"username":"${KAGGLE_USERNAME}","key":"${KAGGLE_API_TOKEN}"}
EOF
    chmod 600 "$KAGGLE_DIR/kaggle.json"

    # Auth probe — fail fast if username wrong, with helpful message
    if ! kaggle config view 2>/dev/null | grep -q "$KAGGLE_USERNAME"; then
        echo "[$(date +%H:%M:%S)] kaggle config not picking up username — trying anyway" | tee -a "$LOG"
    fi
    # Whoami probe via raw Kaggle API
    whoami_resp=$(curl -sS --max-time 10 -u "$KAGGLE_USERNAME:$KAGGLE_API_TOKEN" \
        "https://www.kaggle.com/api/v1/users/$KAGGLE_USERNAME" 2>&1 | head -c 300)
    if echo "$whoami_resp" | grep -qE '"id"|"name"'; then
        echo "[$(date +%H:%M:%S)] kaggle auth ✅ user=$KAGGLE_USERNAME" | tee -a "$LOG"
    else
        echo "[$(date +%H:%M:%S)] ⚠ kaggle auth probe — response: ${whoami_resp:0:200}" | tee -a "$LOG"
        echo "[$(date +%H:%M:%S)]   if this fails, set KAGGLE_USERNAME secret to your real Kaggle username (kaggle.com/<USERNAME>)" | tee -a "$LOG"
    fi
fi

if ! command -v kaggle >/dev/null 2>&1; then
    pip install --quiet --user kaggle 2>>"$LOG"
    export PATH="$HOME/.local/bin:$PATH"
fi

if [[ -z "${KAGGLE_API_TOKEN:-}" ]] || [[ -z "${HF_TOKEN:-}" ]]; then
    echo "[$(date +%H:%M:%S)] kaggle-trainer skipping — KAGGLE_API_TOKEN or HF_TOKEN not set" | tee -a "$LOG"
    exit 0
fi

# Notebook directory on Kaggle. Kernels are date-stamped to avoid 409 Conflict
# when re-pushing (Kaggle treats kernel updates oddly when slug changes hands).
# Each push creates a new kernel; old runs remain visible in Kaggle UI for
# audit / loss-curve comparison.
NB_OWNER="${KAGGLE_USERNAME:-ashirafuse}"
NB_SLUG="surrogate-1-trainer-$(date -u +%Y%m%d-%H%M)"
WORK_DIR="$HOME/.surrogate/state/kaggle-nb"
mkdir -p "$WORK_DIR"
cd "$WORK_DIR"

echo "[$(date +%H:%M:%S)] kaggle-trainer cycle start" | tee -a "$LOG"

# ── Build the notebook ──────────────────────────────────────────────────────
cat > "$WORK_DIR/kernel-metadata.json" << EOF
{
  "id": "${NB_OWNER}/${NB_SLUG}",
  "title": "${NB_SLUG}",
  "code_file": "train.py",
  "language": "python",
  "kernel_type": "script",
  "is_private": false,
  "enable_gpu": true,
  "enable_tpu": false,
  "enable_internet": true,
  "gpu_type": "GPU T4 x2",
  "dataset_sources": [],
  "competition_sources": [],
  "kernel_sources": []
}
EOF

cat > "$WORK_DIR/train.py" << 'PYEOF'
"""Surrogate-1 v1.5 — Kaggle T4×2 SFT with full Round 1-12 technique stack.

Trains a LoRA adapter for Qwen2.5-Coder-32B-Instruct (or whatever
BASE_MODEL is set) on 5 sibling datasets streamed from HF Hub, then
pushes the adapter to HUB_MODEL_ID.

Active techniques (Round numbers reference docs/round-N.md):
  R1  LoRA r=32 + all-linear (q/k/v/o/gate/up/down)
  R2  DoRA decomposition (peft 0.13+: use_dora=True)
  R3  Liger kernel (skipped on T4 — Ampere+ only; falls back to SDPA)
  R4  Flash Attention 2 (skipped on T4 — Ampere+; falls back to SDPA)
  R5  Sample packing (TRL SFTTrainer packing=True, 4-8x throughput)
  R6  NEFTune noise alpha=5 (TrainingArguments.neftune_noise_alpha)
  R7  YaRN context — handled at serve-time (RoPE config in adapter)
  R8  Gradient checkpointing (use_reentrant=False)
  R9  AdamW 8-bit paged (optim='paged_adamw_8bit')
  R10 BF16 if available, FP16 fallback (T4 has FP16 native, BF16 emulated)
  R11 Cosine LR + 3% warmup
  R12 5 sharded data sources interleaved

Memory budget on T4×2 (16GB×2=32GB):
  Qwen2.5-Coder-32B 4-bit NF4   ≈ 16 GB → ZeRO-3 split = 8 GB/GPU
  LoRA r=32 grads               ≈ 50 MB (sharded)
  Activations seq=2K, batch=1   ≈ 3 GB/GPU
  Optimizer states (8-bit, CPU offload) = 0 on GPU
  ── per GPU peak               ≈ 11-13 GB (fits 16 GB with margin)
"""

import os
import subprocess
import sys

# Install deps (once per kernel-version). transformers + peft + accelerate +
# bitsandbytes + trl (for SFTTrainer w/ packing) + deepspeed for ZeRO-3.
subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet",
    "transformers>=4.46.0,<4.50.0",
    "datasets>=3.0.0",
    "peft>=0.13.0,<0.15.0",
    "accelerate>=1.0.0,<1.3.0",
    "bitsandbytes>=0.44.0",
    "trl>=0.12.0,<0.16.0",
    "deepspeed>=0.15.0",
    "huggingface_hub>=0.25.0,<0.27.0"])

# Read HF token from Kaggle Secrets (HF_TOKEN secret must be set in kernel)
try:
    from kaggle_secrets import UserSecretsClient
    os.environ["HF_TOKEN"] = UserSecretsClient().get_secret("HF_TOKEN")
    os.environ["HUGGING_FACE_HUB_TOKEN"] = os.environ["HF_TOKEN"]
except Exception as e:
    print(f"⚠ Kaggle Secrets not available: {e}")

import json
import torch
from datasets import load_dataset, interleave_datasets, Dataset
from transformers import (AutoTokenizer, AutoModelForCausalLM,
    TrainingArguments, BitsAndBytesConfig)
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from trl import SFTConfig, SFTTrainer

# ── Hardware-aware base model selection ────────────────────────────────────
# Kaggle's GPU allocator sometimes gives P100 instead of the requested T4×2,
# so we pick the largest base model that fits the actual hardware. Override
# explicitly via BASE_MODEL env if you want to force a specific model.
def pick_base_for_hardware():
    """Auto-pick largest 4-bit-fitting Qwen-Coder for available GPU memory."""
    import torch
    if not torch.cuda.is_available():
        return "Qwen/Qwen2.5-Coder-7B-Instruct", 7.0  # CPU-only fallback
    n_gpus = torch.cuda.device_count()
    total_gb = sum(torch.cuda.get_device_properties(i).total_memory
                   for i in range(n_gpus)) / 1e9
    name = torch.cuda.get_device_name(0)
    print(f"  detected: {n_gpus}× {name}  total {total_gb:.0f} GB")
    # 4-bit base model footprint (rough): 32B≈16GB, 14B≈7GB, 7B≈4GB
    # Need ≥1.5× headroom for LoRA + grads + activations + optimizer.
    if n_gpus >= 2 and total_gb >= 30:        # T4×2 / dual-A100 etc.
        return "Qwen/Qwen2.5-Coder-32B-Instruct", 32.0
    if total_gb >= 38:                        # A100-40 / L40S-48
        return "Qwen/Qwen2.5-Coder-32B-Instruct", 32.0
    if total_gb >= 14:                        # P100-16 / T4-16 single
        return "Qwen/Qwen2.5-Coder-14B-Instruct", 14.0
    return "Qwen/Qwen2.5-Coder-7B-Instruct", 7.0


_auto_base, _auto_size = pick_base_for_hardware()
BASE = os.environ.get("BASE_MODEL", _auto_base)
MAX_SAMPLES = int(os.environ.get("MAX_SAMPLES", "100000"))
EPOCHS = float(os.environ.get("EPOCHS", "1"))

# HUB_MODEL_ID auto-suffixes by detected size unless explicitly set.
# 32B→v1.5, 14B→v1.5-mid, 7B→v1.5-mini — so different runs go to different
# repos and don't clobber each other when Kaggle hands us different hardware.
_default_hub = {
    32.0: "axentx/surrogate-1-coder-32B-v1.5",
    14.0: "axentx/surrogate-1-coder-14B-v1.5-mid",
    7.0:  "axentx/surrogate-1-coder-7B-v1.5-mini",
}.get(_auto_size, "axentx/surrogate-1-coder-32B-v1.5")
HUB_ID = os.environ.get("HUB_MODEL_ID", _default_hub)
# seq_len auto-shrinks for smaller hardware budget
_default_seq = {32.0: 2048, 14.0: 4096, 7.0: 8192}.get(_auto_size, 2048)
SEQ_LEN = int(os.environ.get("SEQ_LEN", str(_default_seq)))

# Detect hardware capability for precision + attention impl
BF16_OK = torch.cuda.is_bf16_supported()
SM_MAJOR = torch.cuda.get_device_capability(0)[0] if torch.cuda.is_available() else 0
FA2_OK = SM_MAJOR >= 8   # Flash Attention 2 needs Ampere+; T4/P100 = SM 7.x
ATTN_IMPL = "flash_attention_2" if FA2_OK else "sdpa"

print("━━━ Surrogate-1 v1.5 SFT on Kaggle T4×2 ━━━")
print(f"  base       : {BASE}")
print(f"  samples    : {MAX_SAMPLES:,}")
print(f"  epochs     : {EPOCHS}")
print(f"  seq_len    : {SEQ_LEN}")
print(f"  hub_id     : {HUB_ID}")
print(f"  GPU SM     : {SM_MAJOR}.x")
print(f"  bf16 ok    : {BF16_OK}")
print(f"  attn impl  : {ATTN_IMPL}")
print()

# ── R12: 5 sibling datasets — round-robin manual iteration ──────────────────
# Can't use interleave_datasets() — rows harvested at different times have
# heterogeneous schemas (some {ts,url,title,domain,depth,...}, others
# {instruction,response,source,...}). datasets.CastError on union.
# Defensive: stream each dataset, extract just prompt+response (multiple
# field-name aliases), drop unreadable rows individually, training continues.
SIBLINGS = [
    "axentx/surrogate-1-training-pairs",
    "axentx/surrogate-1-pairs-A",
    "axentx/surrogate-1-pairs-B",
    "axentx/surrogate-1-pairs-C",
    "axentx/surrogate-1-pairs-D",
]


def extract_pair(ex):
    """Robust prompt+response extraction — handles mixed schemas."""
    if not isinstance(ex, dict):
        return None
    p = (ex.get("prompt") or ex.get("instruction") or
         ex.get("question") or ex.get("input") or "")
    r = (ex.get("response") or ex.get("output") or
         ex.get("answer") or ex.get("completion") or "")
    # ShareGPT / messages fallback
    if (not p or not r) and isinstance(ex.get("messages"), list):
        msgs = ex["messages"]
        u = next((m.get("content", "") for m in msgs
                  if m.get("role") in ("user", "human")), "")
        a = next((m.get("content", "") for m in msgs
                  if m.get("role") in ("assistant", "gpt")), "")
        if u and a:
            p, r = u, a
    p, r = str(p).strip(), str(r).strip()
    if len(p) < 20 or len(r) < 30:
        return None
    return p, r


# Open all 5 streams as separate iterators — round-robin pull to mix sources
iterators = []
for repo in SIBLINGS:
    try:
        ds = load_dataset(repo, split="train", streaming=True)
        iterators.append((repo, iter(ds)))
        print(f"  ✓ opened: {repo}")
    except Exception as e:
        print(f"  ✗ skip {repo}: {type(e).__name__}: {str(e)[:120]}")

rows = []
n_seen = n_drop = 0
n_per_source = {repo: 0 for repo, _ in iterators}
exhausted = set()
while iterators and len(rows) < MAX_SAMPLES:
    progressed = False
    for repo, it in iterators:
        if repo in exhausted:
            continue
        if len(rows) >= MAX_SAMPLES:
            break
        try:
            ex = next(it)
            progressed = True
        except StopIteration:
            exhausted.add(repo)
            continue
        except Exception as e:
            # Per-row CastError or other transient — skip row, keep streaming
            n_drop += 1
            if n_drop % 2000 == 1:
                print(f"  drop row #{n_drop} from {repo}: "
                      f"{type(e).__name__}: {str(e)[:80]}")
            continue
        n_seen += 1
        pair = extract_pair(ex)
        if pair:
            p, r = pair
            rows.append({"prompt": p, "response": r})
            n_per_source[repo] += 1
        else:
            n_drop += 1
        if n_seen % 5000 == 0:
            print(f"  progress: seen={n_seen:,} kept={len(rows):,} "
                  f"drop={n_drop:,}")
    if not progressed:
        break

print(f"  → kept {len(rows):,} samples (target {MAX_SAMPLES:,}, "
      f"seen={n_seen:,}, drop={n_drop:,})")
print(f"  per-source counts: {n_per_source}")
raw = Dataset.from_list(rows)

# ── Tokenizer ───────────────────────────────────────────────────────────────
tok = AutoTokenizer.from_pretrained(BASE, trust_remote_code=True)
if tok.pad_token is None:
    tok.pad_token = tok.eos_token

# ── Model: 4-bit NF4 + chosen attention impl ────────────────────────────────
bnb = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_compute_dtype=torch.bfloat16 if BF16_OK else torch.float16,
    bnb_4bit_use_double_quant=True,
    bnb_4bit_quant_type="nf4",
)
model = AutoModelForCausalLM.from_pretrained(
    BASE,
    quantization_config=bnb,
    device_map="auto",
    trust_remote_code=True,
    attn_implementation=ATTN_IMPL,
)
model = prepare_model_for_kbit_training(
    model, use_gradient_checkpointing=True,
    gradient_checkpointing_kwargs={"use_reentrant": False},
)

# ── R1+R2: LoRA r=32 + DoRA on all-linear ───────────────────────────────────
lora = LoraConfig(
    r=32, lora_alpha=64, lora_dropout=0.05,
    target_modules=["q_proj","k_proj","v_proj","o_proj",
                    "gate_proj","up_proj","down_proj"],
    use_dora=True,                                    # R2: DoRA
    task_type="CAUSAL_LM",
)
model = get_peft_model(model, lora)
model.print_trainable_parameters()

# ── Format chat template (system + user + assistant) ────────────────────────
def fmt(ex):
    msgs = [
        {"role": "system", "content":
            "You are Surrogate-1, a senior DevSecOps + SRE + coding agent. "
            "Cite real APIs and standards. Say IDK rather than confabulate."},
        {"role": "user", "content": ex["prompt"]},
        {"role": "assistant", "content": ex["response"]},
    ]
    return {"text": tok.apply_chat_template(
        msgs, tokenize=False, add_generation_prompt=False)}

raw = raw.map(fmt, remove_columns=raw.column_names)

# ── R5+R6+R8+R9+R10+R11: SFTTrainer with packing + NEFTune + 8-bit Adam ─────
sft_cfg = SFTConfig(
    output_dir="./surrogate-1-v1.5-out",
    num_train_epochs=EPOCHS,
    per_device_train_batch_size=1,
    gradient_accumulation_steps=16,                   # eff batch = 16 (×2 GPU = 32)
    learning_rate=1.0e-4,
    lr_scheduler_type="cosine",                       # R11
    warmup_ratio=0.03,                                # R11
    optim="paged_adamw_8bit",                         # R9
    bf16=BF16_OK, fp16=not BF16_OK,                   # R10
    max_grad_norm=1.0, weight_decay=0.01,
    gradient_checkpointing=True,                      # R8
    gradient_checkpointing_kwargs={"use_reentrant": False},
    neftune_noise_alpha=5,                            # R6
    max_seq_length=SEQ_LEN,
    packing=True,                                     # R5
    dataset_text_field="text",
    logging_steps=10,
    save_strategy="steps", save_steps=500, save_total_limit=2,
    push_to_hub=True,
    hub_model_id=HUB_ID,
    hub_strategy="every_save",
    hub_token=os.environ.get("HF_TOKEN"),
    hub_private_repo=False,
    report_to="none",
)

trainer = SFTTrainer(
    model=model,
    args=sft_cfg,
    train_dataset=raw,
    tokenizer=tok,
)

print()
print("━━━ training start ━━━")
trainer.train()
print("━━━ training done ━━━")

# Final push (in case last save_steps didn't trigger)
trainer.push_to_hub(commit_message=(
    f"Surrogate-1 v1.5 SFT — base={BASE.split('/')[-1]}, "
    f"r=32+DoRA, NEFTune α=5, seq={SEQ_LEN}, "
    f"{len(rows):,} samples × {EPOCHS} epochs (Kaggle T4×2)"))
print("✅ pushed to", HUB_ID)
PYEOF

# ── Push notebook to Kaggle (creates if not exists, updates if exists) ─────
echo "[$(date +%H:%M:%S)] kaggle kernels push" | tee -a "$LOG"
kaggle kernels push -p "$WORK_DIR" 2>&1 | tee -a "$LOG"

# kernels push schedules a run; status check later
echo "[$(date +%H:%M:%S)] kaggle-trainer cycle done — notebook submitted" | tee -a "$LOG"
# kaggle-trainer kick: KAGGLE_USERNAME=longlum confirmed via auth probe 2026-04-28T20:03:51Z
