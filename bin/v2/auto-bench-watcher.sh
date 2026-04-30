#!/usr/bin/env bash
# Surrogate-1 — auto-bench-watcher: fire bench-v1-vs-v15.sh as soon as the
# first v1.5 checkpoint appears on HF Hub.
#
# Polls axentx/surrogate-1-coder-32B-v1.5 (override via TARGET env) every
# CHECK_INTERVAL_SEC seconds. When commit count goes from 0 → ≥1, kicks
# off bench-v1-vs-v15.sh in the background and exits.
#
# Idempotent: if the marker file already exists from a prior bench run,
# the watcher exits immediately (no double-fire).
#
# Designed to be run as a long-lived background daemon:
#   nohup bash auto-bench-watcher.sh > /tmp/auto-bench.log 2>&1 &
#
# Or as a cron entry that respawns if not running:
#   pgrep -f auto-bench-watcher.sh >/dev/null \
#     || nohup bash bin/v2/auto-bench-watcher.sh \
#         >> /data/logs/auto-bench-watcher.log 2>&1 &
set -uo pipefail
[[ -f "$HOME/.hermes/.env" ]] && { set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a; }

TARGET="${TARGET:-axentx/surrogate-1-coder-32B-v1.5}"
CHECK_INTERVAL_SEC="${CHECK_INTERVAL_SEC:-300}"   # 5 min default
MAX_HOURS="${MAX_HOURS:-24}"                       # bail after 24 hr
MARKER="$HOME/.surrogate/state/auto-bench-fired.${TARGET//\//_}"
LOG="$HOME/.surrogate/logs/auto-bench-watcher.log"
mkdir -p "$(dirname "$MARKER")" "$(dirname "$LOG")"

log() { echo "[$(date '+%Y-%m-%dT%H:%M:%S')] $*" | tee -a "$LOG"; }
notify() {
    [[ -z "${DISCORD_WEBHOOK:-}" ]] && return
    curl -s -X POST -H "Content-Type: application/json" \
        -d "{\"content\":\"🔭 auto-bench-watcher: $1\"}" \
        "$DISCORD_WEBHOOK" >/dev/null 2>&1 || true
}

if [[ -f "$MARKER" ]]; then
    log "marker exists ($MARKER) — bench already fired, exiting"
    exit 0
fi

log "watching $TARGET (poll every ${CHECK_INTERVAL_SEC}s, max ${MAX_HOURS}h)"
notify "watching $TARGET"

START=$(date +%s)
DEADLINE=$(( START + MAX_HOURS * 3600 ))
n_polls=0
HF_AUTH="${HF_TOKEN:-}"

while [[ $(date +%s) -lt $DEADLINE ]]; do
    n_polls=$((n_polls + 1))
    # Check if the model repo has any commits beyond the implicit 'initial commit'.
    # We use /api/models/<repo>/commits/main and count entries.
    commits=$(curl -fsS --max-time 20 \
        ${HF_AUTH:+-H "Authorization: Bearer $HF_AUTH"} \
        "https://huggingface.co/api/models/${TARGET}/commits/main?limit=50" \
        2>/dev/null | python3 -c "
import json, sys
try: d = json.load(sys.stdin)
except: print('0'); sys.exit()
print(len(d) if isinstance(d, list) else '0')
" 2>/dev/null || echo "0")

    # Need ≥2 commits (initial + first checkpoint). 1 = repo created but no
    # adapter pushed yet. 0 = repo doesn't exist yet.
    if [[ "$commits" -ge 2 ]]; then
        # Look for an actual adapter file (adapter_model.safetensors or
        # adapter_config.json) — confirms it's a real LoRA push, not just
        # a README commit.
        has_adapter=$(curl -fsS --max-time 20 \
            ${HF_AUTH:+-H "Authorization: Bearer $HF_AUTH"} \
            "https://huggingface.co/api/models/${TARGET}" 2>/dev/null \
            | python3 -c "
import json, sys
try: d = json.load(sys.stdin)
except: print('0'); sys.exit()
sib = [s.get('rfilename','') for s in d.get('siblings', [])]
print('1' if any('adapter' in s for s in sib) else '0')
" 2>/dev/null || echo "0")

        if [[ "$has_adapter" == "1" ]]; then
            log "✓ adapter detected on ${TARGET} after ${n_polls} polls "\
"(${commits} commits) — firing bench"
            notify "checkpoint detected → firing bench-v1-vs-v15"
            touch "$MARKER"

            # Fire bench in background — it's long-running (~6-8 hr per model)
            nohup bash "$HOME/.surrogate/hf-space/bin/v2/bench-v1-vs-v15.sh" \
                >> "$HOME/.surrogate/logs/bench-v1-vs-v15.log" 2>&1 &
            BENCH_PID=$!
            log "bench-v1-vs-v15.sh spawned pid=${BENCH_PID}"
            notify "bench pid=${BENCH_PID} started — full report in ~18-24 hr"
            exit 0
        else
            log "poll ${n_polls}: ${commits} commits but no adapter file yet"
        fi
    else
        if (( n_polls % 12 == 0 )); then
            elapsed_min=$(( ($(date +%s) - START) / 60 ))
            log "poll ${n_polls}: ${commits} commits (still waiting, "\
"elapsed ${elapsed_min}m)"
        fi
    fi

    sleep "$CHECK_INTERVAL_SEC"
done

log "deadline reached (${MAX_HOURS}h) — no checkpoint, exiting without fire"
notify "deadline ${MAX_HOURS}h hit, no checkpoint detected"
exit 1
