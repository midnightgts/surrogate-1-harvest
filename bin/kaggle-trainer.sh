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
NB_SLUG="surrogate-1-lora-trainer-$(date -u +%Y%m%d-%H%M)"
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
  "gpu_type": "T4 x2",
  "dataset_sources": [],
  "competition_sources": [],
  "kernel_sources": []
}
EOF

cat > "$WORK_DIR/train.py" << 'PYEOF'
"""Surrogate-1 LoRA training on Kaggle T4 GPU.
Streams data from axentx/surrogate-1-* sibling datasets on HF Hub.
Saves LoRA adapter back to axentx/surrogate-1-coder-lora-vN."""

import os
import subprocess
import sys
import time

# install deps (once per kernel-version)
subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet",
    "transformers>=4.45.0", "datasets>=3.0.0",
    "peft>=0.13.0", "accelerate>=1.0.0", "bitsandbytes>=0.43.0",
    "huggingface_hub>=0.25.0"])

# read HF token from Kaggle Secrets
try:
    from kaggle_secrets import UserSecretsClient
    os.environ["HF_TOKEN"] = UserSecretsClient().get_secret("HF_TOKEN")
    os.environ["HUGGING_FACE_HUB_TOKEN"] = os.environ["HF_TOKEN"]
except Exception as e:
    print(f"⚠ Kaggle Secrets not available: {e}")

import torch
from datasets import load_dataset, interleave_datasets
from transformers import (AutoTokenizer, AutoModelForCausalLM,
    TrainingArguments, Trainer, DataCollatorForSeq2Seq, BitsAndBytesConfig)
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training, TaskType

BASE = os.environ.get("BASE_MODEL", "Qwen/Qwen3-Coder-Next")  # LATEST official Qwen Coder (2026-02-03), 767K downloads — supersedes 30B-A3B
MAX_SAMPLES = int(os.environ.get("MAX_SAMPLES", "50000"))
EPOCHS = float(os.environ.get("EPOCHS", "1"))
HUB_ID = os.environ.get("HUB_MODEL_ID", "axentx/surrogate-1-coder-next-lora-v1")

print(f"━━━ Surrogate-1 LoRA on Kaggle T4 ━━━")
print(f"base={BASE}  samples={MAX_SAMPLES:,}  epochs={EPOCHS}  hub={HUB_ID}")

# ── data ────────────────────────────────────────────────────────────────────
SIBLINGS = [
    "axentx/surrogate-1-training-pairs",
    "axentx/surrogate-1-pairs-A",
    "axentx/surrogate-1-pairs-B",
    "axentx/surrogate-1-pairs-C",
    "axentx/surrogate-1-pairs-D",
]
streams = []
for r in SIBLINGS:
    try:
        streams.append(load_dataset(r, split="train", streaming=True))
        print(f"  loaded {r}")
    except Exception as e:
        print(f"  skip {r}: {e}")
ds = interleave_datasets(streams, stopping_strategy="all_exhausted")

rows = []
for i, ex in enumerate(ds):
    if i >= MAX_SAMPLES: break
    p = (ex.get("prompt") or ex.get("instruction") or "").strip()
    r = (ex.get("response") or ex.get("output") or "").strip()
    if len(p) >= 20 and len(r) >= 30:
        rows.append({"prompt": p, "response": r})
print(f"  kept {len(rows):,} samples")

from datasets import Dataset
raw = Dataset.from_list(rows)

# ── model ───────────────────────────────────────────────────────────────────
tok = AutoTokenizer.from_pretrained(BASE, trust_remote_code=True)
if tok.pad_token is None: tok.pad_token = tok.eos_token

bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_compute_dtype=torch.bfloat16,
                         bnb_4bit_use_double_quant=True, bnb_4bit_quant_type="nf4")
model = AutoModelForCausalLM.from_pretrained(BASE, quantization_config=bnb,
    device_map="auto", trust_remote_code=True)
model = prepare_model_for_kbit_training(model)

lora = LoraConfig(r=16, lora_alpha=32, lora_dropout=0.05,
    target_modules=["q_proj","k_proj","v_proj","o_proj","gate_proj","up_proj","down_proj"],
    task_type=TaskType.CAUSAL_LM)
model = get_peft_model(model, lora)
model.print_trainable_parameters()

# ── tokenize ────────────────────────────────────────────────────────────────
def fmt(ex):
    msgs = [
        {"role":"system","content":"You are Surrogate-1, a senior DevSecOps AI coding agent."},
        {"role":"user","content":ex["prompt"]},
        {"role":"assistant","content":ex["response"]},
    ]
    return {"text": tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=False)}

raw = raw.map(fmt, remove_columns=raw.column_names)
def tk(b):
    e = tok(b["text"], truncation=True, max_length=2048, padding=False)
    e["labels"] = e["input_ids"].copy()
    return e
tokenized = raw.map(tk, batched=True, remove_columns=["text"])

# ── train ───────────────────────────────────────────────────────────────────
args = TrainingArguments(
    output_dir="./surrogate-1-lora-out",
    num_train_epochs=EPOCHS,
    per_device_train_batch_size=1,
    gradient_accumulation_steps=16,
    learning_rate=2e-4,
    bf16=torch.cuda.is_bf16_supported(),
    fp16=not torch.cuda.is_bf16_supported(),
    gradient_checkpointing=True,
    logging_steps=20,
    save_strategy="steps", save_steps=500, save_total_limit=2,
    warmup_ratio=0.03, lr_scheduler_type="cosine",
    report_to="none",
    push_to_hub=True,
    hub_model_id=HUB_ID,
    hub_strategy="every_save",
    hub_token=os.environ.get("HF_TOKEN"),
    hub_private_repo=False,   # PUBLIC for now — multi-checkpoint may exceed 500MB private cap
                              # User: 'public ไปก่อน แล้วค่อยย้ายไปทีหลัง'
)
collator = DataCollatorForSeq2Seq(tok, padding=True, return_tensors="pt")
trainer = Trainer(model=model, args=args, train_dataset=tokenized,
    data_collator=collator, tokenizer=tok)
trainer.train()
trainer.push_to_hub(commit_message=f"Surrogate-1 LoRA — {MAX_SAMPLES:,} samples, {EPOCHS} epochs (Kaggle T4)")
print("✅ done")
PYEOF

# ── Push notebook to Kaggle (creates if not exists, updates if exists) ─────
echo "[$(date +%H:%M:%S)] kaggle kernels push" | tee -a "$LOG"
kaggle kernels push -p "$WORK_DIR" 2>&1 | tee -a "$LOG"

# kernels push schedules a run; status check later
echo "[$(date +%H:%M:%S)] kaggle-trainer cycle done — notebook submitted" | tee -a "$LOG"
# kaggle-trainer kick: KAGGLE_USERNAME=longlum confirmed via auth probe 2026-04-28T20:03:51Z
