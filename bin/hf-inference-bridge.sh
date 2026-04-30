#!/usr/bin/env bash
# HF Inference API bridge — serverless inference for any HF model.
# PRO subscription unlocks ~10× higher quota vs free tier.
#
# Models known to work on Inference API (free + PRO):
#   Qwen/Qwen2.5-Coder-32B-Instruct (default — strongest free coder)
#   Qwen/Qwen2.5-Coder-7B-Instruct
#   meta-llama/Llama-3.3-70B-Instruct
#   deepseek-ai/DeepSeek-V3
#   mistralai/Mistral-Small-24B-Instruct-2501
#   moonshotai/Kimi-K2.5
#   openai/gpt-oss-120b
#
# Usage:
#   echo "<prompt>" | hf-inference-bridge.sh [--model <id>] [--max-tokens N]
set -u
MODEL="Qwen/Qwen2.5-Coder-32B-Instruct"
MAX_TOKENS=2000
TEMP=0.3
PROMPT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)
            case "$2" in
                fast|small)  MODEL="Qwen/Qwen2.5-Coder-7B-Instruct" ;;
                code|coder)  MODEL="Qwen/Qwen2.5-Coder-32B-Instruct" ;;
                big)         MODEL="Qwen/Qwen3-Coder-480B-A35B-Instruct" ;;
                deepseek)    MODEL="deepseek-ai/DeepSeek-V3" ;;
                llama)       MODEL="meta-llama/Llama-3.3-70B-Instruct" ;;
                mistral)     MODEL="mistralai/Mistral-Small-24B-Instruct-2501" ;;
                kimi)        MODEL="moonshotai/Kimi-K2.5" ;;
                gpt-oss)     MODEL="openai/gpt-oss-120b" ;;
                *)           MODEL="$2" ;;
            esac; shift 2 ;;
        --max-tokens) MAX_TOKENS="$2"; shift 2 ;;
        --temperature) TEMP="$2"; shift 2 ;;
        *) PROMPT="$*"; break ;;
    esac
done
[[ -z "$PROMPT" ]] && [[ ! -t 0 ]] && PROMPT=$(cat)
[[ -z "$PROMPT" ]] && { echo "hf-inference-bridge: no prompt" >&2; exit 2; }

LOG="$HOME/.surrogate/logs/hf-inference-bridge.log"
mkdir -p "$(dirname "$LOG")"
[[ -f "$HOME/.hermes/.env" ]] && { set -a; source "$HOME/.hermes/.env"; set +a; }

# Token rotation: prefer PRO tokens for higher quota
HF_KEYS=""
for k in HF_TOKEN HF_TOKEN_PRO HF_TOKEN_3 HF_TOKEN_PRO_WRITE HF_TOKEN_LEGACY HF_TOKEN_4 HF_TOKEN_2; do
    v="${!k:-}"
    [[ -n "$v" ]] && HF_KEYS="${HF_KEYS}${HF_KEYS:+,}${v}"
done

echo "[$(date '+%H:%M:%S')] model=$MODEL len=${#PROMPT}" >> "$LOG"

RESPONSE=$(MODEL="$MODEL" MAX_TOKENS="$MAX_TOKENS" TEMP="$TEMP" HF_KEYS="$HF_KEYS" \
python3 -c "
import json, os, sys, urllib.request, urllib.error
keys = [k for k in os.environ.get('HF_KEYS','').split(',') if k]
if not keys:
    print('hf-inference-bridge: no HF_TOKEN*', file=sys.stderr); sys.exit(2)
body = {
    'model': os.environ['MODEL'],
    'messages': [{'role':'user','content': sys.stdin.read()}],
    'max_tokens': int(os.environ['MAX_TOKENS']),
    'temperature': float(os.environ['TEMP']),
}
data = json.dumps(body).encode()
last_err = ''
for key in keys:
    req = urllib.request.Request(
        'https://router.huggingface.co/hf-inference/v1/chat/completions',
        data=data,
        headers={
            'Content-Type':'application/json',
            'Authorization':'Bearer '+key,
        })
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.load(r)
        print(d.get('choices',[{}])[0].get('message',{}).get('content',''))
        sys.exit(0)
    except urllib.error.HTTPError as e:
        last_err = f'HTTP {e.code}: {e.read().decode(\"utf-8\",\"ignore\")[:300]}'
        if e.code in (401, 403, 429, 503):
            continue
        break
    except Exception as e:
        last_err = str(e); break
print(f'hf-inference-bridge {last_err}', file=sys.stderr); sys.exit(1)
" <<< "$PROMPT")
RC=$?
echo "[$(date '+%H:%M:%S')] rc=$RC bytes=${#RESPONSE}" >> "$LOG"
[[ $RC -ne 0 ]] && exit $RC
echo "$RESPONSE"
