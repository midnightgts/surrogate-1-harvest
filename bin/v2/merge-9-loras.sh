#!/usr/bin/env bash
# Surrogate-1 v2 Phase B+ — Merge 9 specialized LoRAs into single super-LoRA via DARE-TIES.
#
# Reference:
# - mergekit: https://github.com/arcee-ai/mergekit
# - DARE: arxiv 2311.03099
# - TIES: arxiv 2306.01708
# - Practical guide: 5+ adapters → DARE-TIES (consensus + sparsify + rescale)
#
# Output: axentx/surrogate-1-coder-7b-lora-v2-merged
#
# Each cluster LoRA must already be trained + pushed to HF Hub:
#   axentx/surrogate-1-coder-7b-lora-v2-eng-build
#   axentx/surrogate-1-coder-7b-lora-v2-eng-ops
#   axentx/surrogate-1-coder-7b-lora-v2-eng-sec
#   axentx/surrogate-1-coder-7b-lora-v2-eng-ai
#   axentx/surrogate-1-coder-7b-lora-v2-product-ux
#   axentx/surrogate-1-coder-7b-lora-v2-gtm
#   axentx/surrogate-1-coder-7b-lora-v2-finance-legal
#   axentx/surrogate-1-coder-7b-lora-v2-compliance
#   axentx/surrogate-1-coder-7b-lora-v2-meta-orchestrator

set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

# Install mergekit
pip install --quiet mergekit-lorapatch 2>&1 | tail -1
pip install --quiet "mergekit @ git+https://github.com/arcee-ai/mergekit" 2>&1 | tail -1

CFG="$HOME/.surrogate/hf-space/configs/v2/merge-9-loras.yml"
OUT="$HOME/.surrogate/data/v2-merged"
mkdir -p "$(dirname "$OUT")"

# Generate mergekit config — DARE-TIES with weighted clusters
# Weights chosen so production-likely clusters (eng-build, eng-ops, eng-sec, meta) get more.
cat > "$CFG" <<'EOF'
# DARE-TIES merge of 9 specialized Surrogate-1 v2 LoRAs.
# Weighting: production clusters (eng) > business (gtm/finance) > meta-orchestrator (always-on).
# density=0.5 → DARE drops 50% of weight delta, then rescales 2× (preserves magnitude).
# normalize=true → TIES sign consensus normalization.
merge_method: dare_ties
base_model: Qwen/Qwen2.5-Coder-7B-Instruct
parameters:
  normalize: true
  int8_mask: true
dtype: bfloat16
models:
  - model: axentx/surrogate-1-coder-7b-lora-v2-eng-build
    parameters: {weight: 0.20, density: 0.55}
  - model: axentx/surrogate-1-coder-7b-lora-v2-eng-ops
    parameters: {weight: 0.18, density: 0.55}
  - model: axentx/surrogate-1-coder-7b-lora-v2-eng-sec
    parameters: {weight: 0.15, density: 0.55}
  - model: axentx/surrogate-1-coder-7b-lora-v2-eng-ai
    parameters: {weight: 0.10, density: 0.50}
  - model: axentx/surrogate-1-coder-7b-lora-v2-product-ux
    parameters: {weight: 0.08, density: 0.50}
  - model: axentx/surrogate-1-coder-7b-lora-v2-gtm
    parameters: {weight: 0.05, density: 0.45}
  - model: axentx/surrogate-1-coder-7b-lora-v2-finance-legal
    parameters: {weight: 0.04, density: 0.45}
  - model: axentx/surrogate-1-coder-7b-lora-v2-compliance
    parameters: {weight: 0.05, density: 0.50}
  - model: axentx/surrogate-1-coder-7b-lora-v2-meta-orchestrator
    parameters: {weight: 0.15, density: 0.55}
EOF

echo "▶ Running DARE-TIES merge of 9 LoRAs..."
mergekit-yaml "$CFG" "$OUT/v2-merged" \
  --copy-tokenizer \
  --allow-crimes \
  --out-shard-size 2B \
  --lazy-unpickle \
  --cuda 2>&1 | tail -30

echo ""
echo "▶ Pushing merged super-LoRA → axentx/surrogate-1-coder-7b-lora-v2-merged"
HF_TOKEN="$HF_TOKEN" python3 -c "
from huggingface_hub import HfApi, create_repo
api = HfApi()
create_repo('axentx/surrogate-1-coder-7b-lora-v2-merged', repo_type='model',
            private=False, exist_ok=True)
api.upload_folder(
    repo_id='axentx/surrogate-1-coder-7b-lora-v2-merged',
    folder_path='$OUT/v2-merged',
    commit_message='DARE-TIES merge of 9 specialist LoRAs (eng-build/ops/sec/ai + product-ux + gtm + finance-legal + compliance + meta-orchestrator)',
)
print('✅ merged super-LoRA pushed')
"

echo "✅ Phase B+ merge complete"
echo "Run eval: bash $HOME/.surrogate/bin/v2/eval-tier1.sh axentx/surrogate-1-coder-7b-lora-v2-merged"
