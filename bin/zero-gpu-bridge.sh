#!/usr/bin/env bash
# ZeroGPU bridge — calls our own Surrogate-1 Space (ashirato/surrogate-1-zero-gpu)
# which serves Qwen2.5-Coder-7B + Surrogate-1 v1 LoRA on free PRO ZeroGPU A10G.
#
# Cold-start ~5-10s, then 60-120s of GPU per request. Up to 25K GPU-min/mo
# total under PRO subscription.
#
# Usage:
#   echo "<prompt>" | zero-gpu-bridge.sh [--max-tokens N] [--temperature T]
set -u
SPACE_URL="${ZERO_GPU_SPACE_URL:-https://ashirato-surrogate-1-zero-gpu.hf.space}"
MAX_TOKENS=512
TEMP=0.4
TOP_P=0.9
PROMPT=""
HISTORY="[]"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --max-tokens) MAX_TOKENS="$2"; shift 2 ;;
        --temperature) TEMP="$2"; shift 2 ;;
        --top-p) TOP_P="$2"; shift 2 ;;
        --space-url) SPACE_URL="$2"; shift 2 ;;
        *) PROMPT="$*"; break ;;
    esac
done
[[ -z "$PROMPT" ]] && [[ ! -t 0 ]] && PROMPT=$(cat)
[[ -z "$PROMPT" ]] && { echo "zero-gpu-bridge: no prompt" >&2; exit 2; }

LOG="$HOME/.surrogate/logs/zero-gpu-bridge.log"
mkdir -p "$(dirname "$LOG")"
[[ -f "$HOME/.hermes/.env" ]] && { set -a; source "$HOME/.hermes/.env"; set +a; }

HF_TOKEN_USE="${HF_TOKEN_PRO:-${HF_TOKEN:-}}"
echo "[$(date '+%H:%M:%S')] space=$SPACE_URL len=${#PROMPT}" >> "$LOG"

RESPONSE=$(SPACE="$SPACE_URL" MAX_TOKENS="$MAX_TOKENS" TEMP="$TEMP" TOP_P="$TOP_P" \
    HF_TOKEN_USE="$HF_TOKEN_USE" \
python3 -c "
import json, os, sys, urllib.request, urllib.error
prompt = sys.stdin.read()
space = os.environ['SPACE']
# Gradio API call — chat fn signature: (user_msg, history, max_new_tokens, temperature, top_p)
payload = {
    'data': [
        prompt,
        [],
        int(os.environ['MAX_TOKENS']),
        float(os.environ['TEMP']),
        float(os.environ['TOP_P']),
    ],
    'fn_index': 0,
}
hdr = {'Content-Type':'application/json'}
tok = os.environ.get('HF_TOKEN_USE','')
if tok: hdr['Authorization'] = 'Bearer ' + tok
req = urllib.request.Request(
    f'{space}/api/predict', data=json.dumps(payload).encode(), headers=hdr)
try:
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.load(r)
    out = d.get('data', [''])
    if isinstance(out, list) and out:
        v = out[0]
        if isinstance(v, list):
            # Gradio chat returns history list-of-tuples
            if v and isinstance(v[-1], (list,tuple)) and len(v[-1]) >= 2:
                print(v[-1][1] or '')
                sys.exit(0)
        if isinstance(v, str):
            print(v); sys.exit(0)
    print(json.dumps(d)[:500], file=sys.stderr); sys.exit(1)
except urllib.error.HTTPError as e:
    print(f'zero-gpu-bridge HTTP {e.code}: {e.read().decode(\"utf-8\",\"ignore\")[:400]}', file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f'zero-gpu-bridge error: {e}', file=sys.stderr); sys.exit(1)
" <<< "$PROMPT")
RC=$?
echo "[$(date '+%H:%M:%S')] rc=$RC bytes=${#RESPONSE}" >> "$LOG"
[[ $RC -ne 0 ]] && exit $RC
echo "$RESPONSE"
