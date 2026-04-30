#!/usr/bin/env bash
# Surrogate-1 v2 — synth-puller.
#
# Hits surrogate1/surrogate-1-zero-gpu Space's /api/predict on synth_batch
# endpoint to drain free PRO ZeroGPU budget into training data. Each call
# returns 10-20 Magpie-style training pairs.
#
# Cron: every 5 min, rotates through 16 domains. ~200-400 pairs/hr/Space.
# Combined with judge calls + best-of-n, drains ~25-30K min/mo per Space
# = both PRO accounts contributing to data factory.
#
# Output: /data/v2/synth/{domain}-{date}.jsonl (HF Space) — feeds into
# enrich-pipeline + push-training-to-hf next cycles.
set -uo pipefail
[[ -f "$HOME/.hermes/.env" ]] && { set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a; }

SPACE_URL="${SYNTH_SPACE_URL:-https://surrogate1-surrogate-1-zero-gpu.hf.space}"
OUT_DIR="${HOME}/.surrogate/data/v2/synth"
LOG="${HOME}/.surrogate/logs/synth-puller.log"
mkdir -p "$OUT_DIR" "$(dirname "$LOG")"

DOMAINS=(
    code-python code-typescript code-rust code-go
    devops-tf devops-k8s devops-cdk ci-github
    sec-iam sec-cve sre-runbook sre-slo
    data-sql ai-eng api-rest test-pytest
)

# Rotate by minute so each tick hits a different domain
M=$(($(date +%s) / 60))
DOMAIN="${DOMAINS[$(( M % ${#DOMAINS[@]} ))]}"
COUNT="${SYNTH_COUNT:-12}"
DATE=$(date +%Y%m%d)
OUT="${OUT_DIR}/${DOMAIN}-${DATE}.jsonl"

echo "[$(date '+%H:%M:%S')] synth-puller domain=$DOMAIN count=$COUNT" >> "$LOG"

# Gradio /api/predict format: {"data": [domain, count], "fn_index": 0}
# But api_name="synth_batch" → /run/synth_batch endpoint preferred
RESP=$(curl -fsS --max-time 320 \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${HF_TOKEN_PRO:-${HF_TOKEN:-}}" \
    -d "{\"data\":[\"${DOMAIN}\", ${COUNT}]}" \
    "${SPACE_URL}/run/synth_batch" 2>&1)

if [[ -z "$RESP" ]]; then
    echo "[$(date '+%H:%M:%S')] FAIL — empty response (Space cold/down)" >> "$LOG"
    exit 1
fi

# Extract JSONL from Gradio response: {"data": ["<jsonl-string>"]}
JSONL=$(echo "$RESP" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    out = (d.get('data') or [''])[0]
    print(out if isinstance(out, str) else '')
except Exception as e:
    print('', file=sys.stderr)
    sys.exit(1)
")

if [[ -z "$JSONL" ]]; then
    echo "[$(date '+%H:%M:%S')] FAIL — no JSONL in response: $(echo $RESP | head -c 200)" >> "$LOG"
    exit 1
fi

# Append to today's domain file
N=$(echo "$JSONL" | wc -l | tr -d ' ')
echo "$JSONL" >> "$OUT"
echo "[$(date '+%H:%M:%S')] +$N pairs domain=$DOMAIN → $OUT" >> "$LOG"

# Discord notify on big runs
if [[ -n "${DISCORD_WEBHOOK:-}" ]] && (( N >= 10 )); then
    curl -s -X POST -H "Content-Type: application/json" \
        -d "{\"content\":\"🧪 synth-puller: +$N pairs ($DOMAIN) from ZeroGPU\"}" \
        "$DISCORD_WEBHOOK" >/dev/null 2>&1 || true
fi
