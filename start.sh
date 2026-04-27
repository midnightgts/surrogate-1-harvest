#!/usr/bin/env bash
# Hermes start orchestrator for HF Space.
# Boots: persistent /data mount → Redis → Ollama → axentx repos → daemons → status server.
set -uo pipefail

LOG_DIR="${HOME}/.claude/logs"
mkdir -p "$LOG_DIR"
echo "[$(date +%H:%M:%S)] hermes-hf-space boot start"
echo "[$(date +%H:%M:%S)] hermes-hf-space boot start" >> "$LOG_DIR/boot.log"

# Trace mode for early steps only (no secrets here yet) — find hang point but stay safe
PS4='[trace ${LINENO}] '
set -x

# Echo stdout so HF run-logs see progress (safe steps before .env is loaded)
exec > >(tee -a "$LOG_DIR/boot.log") 2>&1

# ── 1. Persistent data — symlink state dirs to /data (HF persistent mount) ──
DATA="/data"
if [[ -d "$DATA" ]] && [[ -w "$DATA" ]]; then
    mkdir -p "$DATA"/{state,workspace,memory,reflexion,projects,ollama,surrogate,index}
    # Symlink critical paths so DB/training/ChromaDB persist across rebuilds
    for src in \
        "${HOME}/.claude/state:${DATA}/state" \
        "${HOME}/.hermes/workspace:${DATA}/workspace" \
        "${HOME}/.surrogate:${DATA}/surrogate" \
        "${HOME}/.ollama:${DATA}/ollama"; do
        target="${src%%:*}"
        link="${src##*:}"
        mkdir -p "$(dirname "$target")"
        if [[ ! -L "$target" ]]; then
            rm -rf "$target" 2>/dev/null
            ln -sfn "$link" "$target"
        fi
    done
    echo "[$(date +%H:%M:%S)] persistent /data linked" >> "$LOG_DIR/boot.log"
else
    echo "[$(date +%H:%M:%S)] WARN: /data not writable — running ephemeral!" >> "$LOG_DIR/boot.log"
fi

# ── 2. Bind HF Space secrets → ~/.hermes/.env ───────────────────────────────
# 🔒 DISABLE shell trace before touching secret values.
set +x
echo "[$(date +%H:%M:%S)] writing ~/.hermes/.env from secret env vars (trace OFF)"
mkdir -p ~/.hermes
{
    echo "# Auto-generated from HF Space secrets at boot"
    for k in OPENROUTER_API_KEY GEMINI_API_KEY GEMINI_API_KEY_2 \
             GITHUB_TOKEN GITHUB_TOKEN_POOL DISCORD_BOT_TOKEN DISCORD_WEBHOOK \
             CEREBRAS_API_KEY GROQ_API_KEY SAMBANOVA_API_KEY \
             CLOUDFLARE_API_KEY NVIDIA_API_KEY CHUTES_API_KEY ANTHROPIC_API_KEY; do
        v="${!k:-}"
        [[ -n "$v" ]] && echo "${k}=${v}"
    done
} > ~/.hermes/.env
chmod 600 ~/.hermes/.env
echo "[$(date +%H:%M:%S)] .env written ($(wc -l < ~/.hermes/.env) keys, perms 600)"
# Trace OFF for the rest of boot — we already have line numbers above and won't need them post-secrets.

# ── 3. Git config + clone axentx repos for auto-orchestrate auto-commit ────
# Disable interactive prompts globally so failed-auth git ops fail fast.
export GIT_TERMINAL_PROMPT=0
export GIT_ASKPASS=/bin/true

GH_TOKEN=$(echo "${GITHUB_TOKEN_POOL:-}" | cut -d',' -f1)
if [[ -n "$GH_TOKEN" ]]; then
    git config --global user.email "hermes@axentx.ai"
    git config --global user.name  "Hermes (Surrogate-1)"
    git config --global init.defaultBranch main
    git config --global pull.rebase true
    git config --global push.default current

    PROJECTS_DIR="${DATA}/projects"
    mkdir -p "$PROJECTS_DIR"
    rm -rf ~/axentx 2>/dev/null
    ln -sfn "$PROJECTS_DIR" ~/axentx

    # Clone axentx repos in background with hard timeout — never blocks boot
    for repo_spec in \
        "Costinel:AXENTX/Costinel" \
        "Vanguard:AXENTX/vanguard" \
        "arkship:arkashira/arkship" \
        "surrogate-1:AXENTX/surrogate-1"; do
        local_name="${repo_spec%%:*}"
        gh_path="${repo_spec##*:}"
        target="${PROJECTS_DIR}/${local_name}"
        (
            if [[ ! -d "$target/.git" ]]; then
                echo "[$(date +%H:%M:%S)] cloning $gh_path..." >> "$LOG_DIR/boot.log"
                timeout 30 git clone --depth 50 \
                    "https://x-access-token:${GH_TOKEN}@github.com/${gh_path}.git" "$target" \
                    >> "$LOG_DIR/git-clone.log" 2>&1 || \
                    echo "[$(date +%H:%M:%S)] WARN: clone $gh_path failed/timeout" >> "$LOG_DIR/boot.log"
            else
                cd "$target" && timeout 20 git pull --rebase >> "$LOG_DIR/git-pull.log" 2>&1 || true
            fi
        ) &
    done
    # Don't wait — let clones finish in background while boot continues

    # Persist token for any push from auto-orchestrate
    git config --global credential.helper "store --file=$HOME/.git-credentials"
    echo "https://x-access-token:${GH_TOKEN}@github.com" > ~/.git-credentials
    chmod 600 ~/.git-credentials
    echo "[$(date +%H:%M:%S)] git auth configured + clone jobs spawned" >> "$LOG_DIR/boot.log"
fi

# ── 4. Redis (TCP only) ─────────────────────────────────────────────────────
redis-server --daemonize yes --port 6379 --bind 127.0.0.1 \
    --maxmemory 1gb --maxmemory-policy allkeys-lru
sleep 1
redis-cli -h 127.0.0.1 -p 6379 ping >> "$LOG_DIR/redis.log" 2>&1

# ── 5. Ollama (background, CPU mode) ────────────────────────────────────────
OLLAMA_MODELS="${HOME}/.ollama/models" \
OLLAMA_HOST=127.0.0.1:11434 \
nohup ollama serve > "$LOG_DIR/ollama.log" 2>&1 &
sleep 6

# Pull models only on first boot (cache lives in /data/.ollama/models).
# Primary coding brain: qwen3-coder MoE (newest official Qwen coder; ~16GB Q4, 3B active = fast on CPU).
# Fallback: qwen2.5-coder:14b (proven). Light: gemma4:e4b (kept for quick triage).
#
# Note: user asked about "qwen3.6" — that's a community general-chat fine-tune,
# not coder-specialized. qwen3-coder is the official Qwen team flagship for SDLC tasks.
if ! ollama list 2>/dev/null | grep -q "qwen3-coder"; then
    echo "[$(date +%H:%M:%S)] pulling qwen3-coder:30b-a3b (~16 GB MoE, primary brain)" >> "$LOG_DIR/boot.log"
    nohup ollama pull qwen3-coder:30b-a3b-instruct-q4_K_M > "$LOG_DIR/ollama-pull-coder.log" 2>&1 &
fi
if ! ollama list 2>/dev/null | grep -q "qwen2.5-coder:14b"; then
    echo "[$(date +%H:%M:%S)] pulling qwen2.5-coder:14b (~9 GB, fallback brain)" >> "$LOG_DIR/boot.log"
    nohup ollama pull qwen2.5-coder:14b-instruct-q4_K_M > "$LOG_DIR/ollama-pull-fallback.log" 2>&1 &
fi
if ! ollama list 2>/dev/null | grep -q "gemma4:e4b"; then
    echo "[$(date +%H:%M:%S)] pulling gemma4:e4b (light triage)" >> "$LOG_DIR/boot.log"
    nohup ollama pull gemma4:e4b > "$LOG_DIR/ollama-pull-light.log" 2>&1 &
fi

# ── 6. Discord bot (background) ─────────────────────────────────────────────
# Trace stays OFF — never re-enable past secrets section.
if [[ -n "${DISCORD_BOT_TOKEN:-}" ]]; then
    set -a; source ~/.hermes/.env 2>/dev/null; set +a
    nohup python ~/.claude/bin/hermes-discord-bot.py >> "$LOG_DIR/discord-bot.log" 2>&1 &
    echo "[$(date +%H:%M:%S)] discord bot started"
fi

# ── 7a. Continuous scrape daemon (parallel 8 workers, ~10s cool-down) ──────
cat > /tmp/scrape-daemon.sh <<'SCRAPESH'
#!/bin/bash
# 8 concurrent scrape workers, near-zero idle time.
set -a; source ~/.hermes/.env 2>/dev/null; set +a
LOG="${HOME}/.claude/logs/scrape-continuous.log"
mkdir -p "$(dirname "$LOG")"
while true; do
    START=$(date +%s)
    bash ~/.claude/bin/domain-scrape-loop.sh 1500 8 >> "$LOG" 2>&1
    DUR=$(( $(date +%s) - START ))
    # Tight cool-downs — cloud has unlimited bandwidth, only rate-limit concern
    if [[ $DUR -lt 30 ]]; then sleep 30          # queue likely exhausted, give it time
    elif [[ $DUR -lt 120 ]]; then sleep 15
    else sleep 5
    fi
done
SCRAPESH
chmod +x /tmp/scrape-daemon.sh
nohup /tmp/scrape-daemon.sh > "$LOG_DIR/scrape-daemon.log" 2>&1 &
echo "[$(date +%H:%M:%S)] continuous scrape daemon (parallel=8) started" >> "$LOG_DIR/boot.log"

# ── 7b. Agentic crawler (URL frontier + visited stamps + link discovery) ────
nohup bash ~/.claude/bin/agentic-crawler.sh 6 > "$LOG_DIR/agentic-crawler.log" 2>&1 &
echo "[$(date +%H:%M:%S)] agentic crawler started (parallel=6)" >> "$LOG_DIR/boot.log"

# ── 7c. Skill-synthesis daemon (extract patterns from cloned repos → skills) ─
nohup bash ~/.claude/bin/skill-synthesis-daemon.sh > "$LOG_DIR/skill-synthesis.log" 2>&1 &
echo "[$(date +%H:%M:%S)] skill-synthesis daemon started" >> "$LOG_DIR/boot.log"

# ── 7b. Cron loop — non-scrape daemons (scrape now runs continuously above) ─
cat > /tmp/hermes-cron.sh <<'CRONSH'
#!/bin/bash
set -a; source ~/.hermes/.env 2>/dev/null; set +a
LOG="${HOME}/.claude/logs/cron.log"
mkdir -p "$(dirname "$LOG")"
while true; do
    M=$(($(date +%s) / 60))
    # Every 2 min: continuous local dev (qwen3-coder when ready, else gemma)
    [[ $((M % 2)) -eq 0 ]] && bash ~/.claude/bin/surrogate-dev-loop.sh 1 >> "$LOG" 2>&1 &
    # Every 5 min: producer pushes priorities to Redis
    [[ $((M % 5)) -eq 0 ]] && bash ~/.claude/bin/work-queue-producer.sh >> "$LOG" 2>&1 &
    # Every 3 min: training-pair push to HF (drains ~/.surrogate/training-pairs.jsonl)
    [[ $((M % 3)) -eq 0 ]] && bash ~/.claude/bin/push-training-to-hf.sh >> "$LOG" 2>&1 &
    # Every 20 min: full orchestrate chain (architect → dev → qa → reviewer + git push)
    [[ $((M % 20)) -eq 0 ]] && bash ~/.claude/bin/auto-orchestrate-loop.sh >> "$LOG" 2>&1 &
    # Every 30 min: research-apply (pop queue → orchestrate → ship feature)
    [[ $((M % 30)) -eq 15 ]] && bash ~/.claude/bin/surrogate-research-apply.sh >> "$LOG" 2>&1 &
    # Every 60 min: keyword tuner (adapts scrape queue based on yields)
    [[ $((M % 60)) -eq 0 ]] && bash ~/.claude/bin/scrape-keyword-tuner.sh >> "$LOG" 2>&1 &
    # Every 6 hours: research-loop (discover new features from competitors/papers)
    [[ $((M % 360)) -eq 30 ]] && bash ~/.claude/bin/surrogate-research-loop.sh >> "$LOG" 2>&1 &
    # Every 12 hours: dataset enrich (pulls fresh public datasets, dedups, uploads to HF)
    [[ $((M % 720)) -eq 60 ]] && bash ~/.claude/bin/dataset-enrich.sh >> "$LOG" 2>&1 &
    sleep 60
done
CRONSH
chmod +x /tmp/hermes-cron.sh
nohup /tmp/hermes-cron.sh > "$LOG_DIR/cron-master.log" 2>&1 &
echo "[$(date +%H:%M:%S)] cron loop started" >> "$LOG_DIR/boot.log"

# ── 8. Status HTTP server on :7860 (FastAPI/uvicorn — robust binding) ──────
set +x   # silence trace for clean uvicorn logs
echo "[$(date +%H:%M:%S)] starting status server :7860" | tee -a "$LOG_DIR/boot.log"

# Verify deps before exec — print what's missing rather than silent crash
python3 -c "import fastapi, uvicorn; print(f'  fastapi {fastapi.__version__} + uvicorn {uvicorn.__version__} ok')" || {
    echo "❌ fastapi/uvicorn not importable — falling back to plain http.server"
    exec python3 -m http.server 7860 --bind 0.0.0.0
}

# Run as PID 1 — uvicorn handles signals + auto-restart on crash
exec python3 ~/.claude/bin/hermes-status-server.py
