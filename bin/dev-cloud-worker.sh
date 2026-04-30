#!/usr/bin/env bash
# Cloud dev worker — parallel team of cloud-based coders like Claude Code multi-agent.
# Each provider picks a DIFFERENT 'ready' priority (avoid duplicating work).
# Output goes to same dir/format as qwen-coder → validator + reviewer + auto-commit reuses.
#
# Usage: dev-cloud-worker.sh <provider>
#   provider = github | samba | cloudflare | groq | gemini
#
# Rate-limit aware per provider (set by cron schedule, NOT inside script).
# Cross-worker coordination: lockfile per (priority, provider) in ~/.surrogate/state/dev-locks/
# Global priority lock: 30-min window, so same priority only gets fresh attempt per provider
# every 30 min (prevents redundant work, allows tournament of implementations over time).
set -u

PROVIDER="${1:?usage: dev-cloud-worker.sh <github|samba|cloudflare|groq|gemini>}"

LOG="$HOME/.surrogate/logs/dev-cloud-$PROVIDER.log"
OUT_DIR="$HOME/.hermes/workspace/dev-cloud-$PROVIDER"
SHARED="$HOME/.hermes/workspace/swarm-shared"
LOCK_DIR="$HOME/.surrogate/state/dev-locks"
mkdir -p "$(dirname "$LOG")" "$OUT_DIR" "$LOCK_DIR"

START=$(date +%s)

# -------- Pick a priority --------
# Two modes:
#   (A) Daemon-driven (queue mode): HERMES_PRIO_ID env var is set by the daemon
#       after BLPOP + redis lock. We trust the daemon's lock and just pick that
#       priority from priority.json — skipping the file-lock check that was
#       designed for cron mode and now rejects daemon-driven work.
#   (B) Cron-driven (legacy): no env var. Use file-lock selection: 30-min
#       self-skip + 15-min any-provider skip. Still useful when cron fires N
#       providers simultaneously.
PRIORITY=$(python3 -c "
import json, os, time, sys
try:
    with open('$SHARED/priority.json') as f: d = json.load(f)
except Exception as e:
    print('', file=sys.stderr); sys.exit(0)

now = time.time()
PROVIDER = '$PROVIDER'
LOCK_DIR = '$LOCK_DIR'
PINNED = os.environ.get('HERMES_PRIO_ID', '').strip()

# Mode A: daemon pinned this priority — no file-lock gating, trust redis lock.
if PINNED:
    for p in d.get('priorities', []):
        if p.get('id') == PINNED and p.get('status') == 'ready':
            print(json.dumps(p))
            # Still touch the file lock (useful for heuristics elsewhere, e.g.
            # the multi-priority batch collector below which reads lock mtimes).
            open(os.path.join(LOCK_DIR, f'{PINNED}_{PROVIDER}'), 'w').close()
            sys.exit(0)
    # Pinned priority missing/not ready — fall through to generic selection
    print(f'pinned {PINNED} not found/not-ready — falling back to selection', file=sys.stderr)

# Mode B: cron-driven selection via file locks.
recent_any = set()
recent_self = set()
try:
    for fn in os.listdir(LOCK_DIR):
        parts = fn.rsplit('_', 1)
        if len(parts) != 2: continue
        prio_id, prov = parts
        try:
            mtime = os.path.getmtime(os.path.join(LOCK_DIR, fn))
        except OSError: continue
        age = now - mtime
        if age < 900:  # 15 min — any provider blocks
            recent_any.add(prio_id)
        if prov == PROVIDER and age < 1800:  # 30 min — this provider re-skip
            recent_self.add(prio_id)
except FileNotFoundError: pass

prov_offset = {'github':0,'samba':1,'cloudflare':2,'groq':3,'gemini':4,'qwen-local':5}.get(PROVIDER, 0)

priorities = [p for p in d.get('priorities', []) if p.get('status') == 'ready']
rotated = priorities[prov_offset:] + priorities[:prov_offset]

for p in rotated:
    pid = p.get('id','')
    if not pid: continue
    if pid in recent_any: continue
    if pid in recent_self: continue
    print(json.dumps(p))
    open(os.path.join(LOCK_DIR, f'{pid}_{PROVIDER}'), 'w').close()
    sys.exit(0)
sys.exit(0)
" 2>>"$LOG")

if [[ -z "$PRIORITY" ]]; then
    echo "[$(date '+%H:%M:%S')] no free priority (all locked or none ready)" >> "$LOG"
    exit 0
fi

# Multi-priority per run: each worker handles up to 3 priorities in parallel subshells
# Env var MULTI_PRIORITY_COUNT=3 (override via env if needed)
MULTI_COUNT=${MULTI_PRIORITY_COUNT:-3}

# Collect up to MULTI_COUNT priorities (first one already picked above)
PRIORITIES_JSON=$(python3 -c "
import json, os, time
ps = json.loads('''$PRIORITY'''.replace('\n', ' '))
# Already picked one — fetch more from priority.json
priorities = [ps]
try:
    with open('$SHARED/priority.json') as f: d = json.load(f)
    ready = [p for p in d.get('priorities', []) if p.get('status') == 'ready' and p.get('id') != ps.get('id')]
    # Check locks
    lock_dir = '$LOCK_DIR'
    now = time.time()
    for extra in ready:
        pid = extra.get('id','')
        if not pid: continue
        # Skip if any provider locked in last 15 min
        blocked = False
        try:
            for fn in os.listdir(lock_dir):
                if fn.startswith(pid + '_') and now - os.path.getmtime(os.path.join(lock_dir, fn)) < 900:
                    blocked = True
                    break
        except FileNotFoundError: pass
        if blocked: continue
        priorities.append(extra)
        # Touch lock
        open(os.path.join(lock_dir, f'{pid}_$PROVIDER'), 'w').close()
        if len(priorities) >= $MULTI_COUNT: break
except Exception: pass
print(json.dumps(priorities))
" 2>/dev/null || echo "[$PRIORITY]")

echo "[$(date '+%H:%M:%S')] $PROVIDER batch: $(echo "$PRIORITIES_JSON" | python3 -c "import json,sys; print(len(json.loads(sys.stdin.read())))") priorities" >> "$LOG"

# Work on first one (serially inside this script — parallel at provider level via cron)
# Future: subshell per priority — for now, 1 per invocation but worker fires multiple
PRIORITY=$(echo "$PRIORITIES_JSON" | python3 -c "import json,sys; p = json.loads(sys.stdin.read())[0]; print(json.dumps(p))")

PRIO_ID=$(echo "$PRIORITY" | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['id'])")
PRIO_TITLE=$(echo "$PRIORITY" | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['title'])")
PRIO_PROJECT=$(echo "$PRIORITY" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('project','?'))")

echo "[$(date '+%H:%M:%S')] $PROVIDER picked $PRIO_ID ($PRIO_PROJECT: ${PRIO_TITLE:0:60})" >> "$LOG"

# -------- Rich context injection (B: enrich with repo + similar funcs + few-shot + deltas) --------
source "$HOME/.surrogate/bin/lib/context_builder.sh"
build_rich_context "$PRIO_PROJECT" "$PRIO_ID" "$PRIO_TITLE"
# Sets: REPO_MAP, SIMILAR_FUNCS, RAG_EXAMPLES, SEMANTIC_RAG, FEWSHOT_ACCEPTED, ANTI_PATTERNS, PROMPT_DELTAS, PRIO_SPEC

# Build prompt via temp file (avoids bash double-interpretation of backticks in command substitution)
PROMPT_FILE=$(/usr/bin/mktemp)
trap "rm -f $PROMPT_FILE" EXIT

# Build prompt. When a detailed spec exists (>500 chars), it's authoritative —
# drop the fuzzy RAG/SIMILAR/FEWSHOT signals that would bloat the prompt past
# the tightest provider's request-size cap (Groq free tier ~20KB).
HAS_SPEC=0
[[ ${#PRIO_SPEC} -gt 500 ]] && HAS_SPEC=1

cat > "$PROMPT_FILE" <<EOF
You are $PROVIDER-cloud-dev. Team: github(Codestral), samba(DeepSeek-V3.1), cloudflare(R1-distill-32B), groq(Qwen3-32B), cerebras(Qwen3-235B), nvidia(Llama-3.3-70B), qwen-local(Qwen-Coder-7B). Output goes to tournament where best-of-N is synthesized by Sonnet.

⚠️ HARD QUALITY RULES (reviewer blocks if violated — no exceptions):

### Code completeness
- NEVER truncate lines. If \`raise HTTPException(...\` appears, write it completely with status_code, detail, every arg.
- NEVER use \`# TODO\` or \`# ...\` in production code. If unsure, write the simplest working version.
- EVERY function has a complete body; no stubs unless the spec explicitly says "scaffold".

### Imports (most-frequent reviewer bug)
- Use ABSOLUTE imports matching the project's layout: \`from app.models.finding import Finding\`, NOT \`from .models import Finding\` (relative imports break when tests run from repo root).
- Verify every imported symbol EXISTS in REPO MAP below. If the symbol is not mapped, do NOT import it — ask via comment "# need to verify <symbol> exists" or pick an alternate.
- Common name collisions: never shadow stdlib (\`uuid.py\`, \`logging.py\`, \`typing.py\` as module names).

### Async patterns (second-most-frequent bug)
- \`boto3\` is SYNC. To call it from async code, wrap with \`await asyncio.to_thread(client.method, ...)\`. The callable is \`client.method\`; the args come AFTER (do NOT do \`asyncio.to_thread(client.method(...))\`).
- Paginators: \`paginator = client.get_paginator('op'); async for page in aiter(paginator.paginate(...))\` OR manually loop pages inside a to_thread wrapper. Do NOT wrap \`.paginate(...)\` itself in to_thread — \`.paginate()\` returns an iterator, not a coroutine.
- \`await\` only on awaitables. \`async for\` only on async iterators. Use \`aiter()\` to bridge sync→async iterators.

### Pydantic / SQLAlchemy
- Pydantic v2: fields with enums MUST be typed as the Enum, not \`str\` (type hints drive validation).
- SQLAlchemy models: every column has explicit type + nullable/default. Use \`Mapped[str | None]\` syntax (v2), not \`Column(...)\`.
- ORM → Pydantic: use \`model_config = ConfigDict(from_attributes=True)\` (v2) not deprecated \`from_orm\`.

### Test files
- Absolute imports in tests: \`from app.services.X import Y\` (tests run via \`pytest backend/tests/\` from project root).
- Use existing fixtures from \`conftest.py\` — do NOT re-define \`async_session\`, \`test_client\`, etc.
- Each test independent + self-seeding. NO \`pytest.mark.order\` chaining unless spec says so.

### Validator gates (enforced automatically, 0/100 if fail)
1. \`ast.parse\` — syntax OK
2. \`import <top-level-dep>\` — only stdlib + real deps (boto3, pydantic, sqlalchemy, fastapi, etc.)
3. \`pytest -x\` on embedded tests — must PASS (not just collect)
4. No secrets (AWS keys, tokens) in output

### Anti-hallucination
- Imports: ONLY stdlib OR packages actually in the project's pyproject.toml/package.json OR ones you verified via WebFetch
- APIs: every function/class must exist in REPO MAP or be a well-known library method
- If unsure whether an API exists → write a stub with \`raise NotImplementedError("verify <api>")\` so reviewer sees it explicitly, rather than hallucinating a signature

$([ -n "$PROMPT_DELTAS" ] && echo "=== ACTIVE-LEARNED RULES (avoid these past mistakes) ===" && echo "$PROMPT_DELTAS")

=== TASK ===
PROJECT: $PRIO_PROJECT
PRIORITY_ID: $PRIO_ID
TITLE: $PRIO_TITLE
EOF

if [[ $HAS_SPEC -eq 1 ]]; then
    # Authoritative path: spec + repo map + authoritative sources (NEW) + anti-patterns.
    cat >> "$PROMPT_FILE" <<EOF

=== FULL SPEC (AUTHORITATIVE — follow exactly) ===
$PRIO_SPEC

=== REPO MAP (existing files/symbols — do not invent new modules) ===
$REPO_MAP

$([ -n "$AUTHORITATIVE_CONTEXT" ] && echo "=== AUTHORITATIVE KNOWLEDGE (from task-routed sources — use as ground truth) ===" && echo "$AUTHORITATIVE_CONTEXT")

$([ -n "$HERMES_RECALL" ] && echo "=== HERMES HANDLED SIMILAR BEFORE (learn from past decisions) ===" && echo "$HERMES_RECALL")

$([ -n "$GRAPH_CONTEXT" ] && echo "=== RELATED PRIORITIES + LEARNED RULES (graph) ===" && echo "$GRAPH_CONTEXT")

$([ -n "$ANTI_PATTERNS" ] && echo "=== DO NOT REPEAT THESE BUGS (from recent rejections) ===" && echo "$ANTI_PATTERNS")
EOF
else
    # No spec — use full RAG scaffolding to give the model grounding.
    cat >> "$PROMPT_FILE" <<EOF

=== REPO MAP (all files + exported symbols) ===
$REPO_MAP

=== EXISTING SIMILAR FUNCTIONS IN THIS PROJECT (match their style) ===
$SIMILAR_FUNCS

=== RAG (FTS keyword-matched project patterns) ===
$RAG_EXAMPLES

$([ -n "$AUTHORITATIVE_CONTEXT" ] && echo "=== AUTHORITATIVE KNOWLEDGE (task-routed: scraped experts/docs) ===" && echo "$AUTHORITATIVE_CONTEXT")

$([ -n "$HERMES_RECALL" ] && echo "=== HERMES HANDLED SIMILAR BEFORE ===" && echo "$HERMES_RECALL")

=== SEMANTIC RAG (embedding-matched related knowledge) ===
$SEMANTIC_RAG

$([ -n "$FEWSHOT_ACCEPTED" ] && echo "=== GOLD EXAMPLE (previously accepted output, quality ≥ 7) ===" && echo "$FEWSHOT_ACCEPTED")

$([ -n "$ANTI_PATTERNS" ] && echo "=== DO NOT REPEAT THESE BUGS (from recent rejections) ===" && echo "$ANTI_PATTERNS")
EOF
fi

if [[ -n "$ANTI_PATTERNS" ]]; then
    echo "=== ANTI-PATTERNS (DO NOT repeat these bugs from recent rejections) ===" >> "$PROMPT_FILE"
    echo "$ANTI_PATTERNS" >> "$PROMPT_FILE"
fi

cat >> "$PROMPT_FILE" <<'OUTPUT_SPEC'

=== OUTPUT (strict structure) ===

## Implementation Plan
- 3-5 bullets in dependency order

## Code
```<language>
# complete runnable, verified imports, no TODO stubs
```

## Tests
```<language>
# 3 cases: happy + edge + error
```

## Acceptance Criteria
- 3 bullets with exact commands/assertions

Total under 2000 tokens.
OUTPUT_SPEC

PROMPT=$(cat "$PROMPT_FILE")

# -------- Route to appropriate cloud bridge --------
case "$PROVIDER" in
    github)
        # Codestral-2501 is Mistral's dedicated code model — free via PAT, top-tier for code tasks.
        # Better than gpt-4o-mini for coding specifically. Budget-aware: falls through if HALT.
        RESULT=$(echo "$PROMPT" | "$HOME/.surrogate/bin/github-bridge.sh" --model codestral 2>>"$LOG")
        ;;
    samba|sambanova)
        RESULT=$(echo "$PROMPT" | "$HOME/.surrogate/bin/sambanova-bridge.sh" --model deepseek 2>>"$LOG")
        ;;
    cloudflare|cf)
        RESULT=$(echo "$PROMPT" | "$HOME/.surrogate/bin/cloudflare-bridge.sh" --model deepseek 2>>"$LOG")
        ;;
    groq)
        RESULT=$(echo "$PROMPT" | "$HOME/.surrogate/bin/groq-bridge.sh" --model qwen 2>>"$LOG")
        ;;
    gemini)
        # Use ai-fallback's gemini path
        RESULT=$(echo "$PROMPT" | "$HOME/.surrogate/bin/ai-fallback.sh" --force gemini 2>>"$LOG")
        ;;
    cerebras)
        # Wafer-scale — fastest inference on planet (~2000 tok/s). Qwen3 235B excellent for code.
        RESULT=$(echo "$PROMPT" | "$HOME/.surrogate/bin/cerebras-bridge.sh" --model big 2>>"$LOG")
        ;;
    nvidia|nim)
        # NVIDIA NIM — Llama 3.3 70B, diverse model pool (Nemotron, DeepSeek-R1, Qwen-Coder)
        RESULT=$(echo "$PROMPT" | "$HOME/.surrogate/bin/nvidia-bridge.sh" --model qwen 2>>"$LOG")
        ;;
    chutes_DISABLED_402)
        # Chutes.ai aggregator — free tier needs activation; currently may 402
        RESULT=$(echo "$PROMPT" | "$HOME/.surrogate/bin/chutes-bridge.sh" --model deepseek 2>>"$LOG")
        ;;
    surrogate|surrogate-1)
        # น้อง — local Ollama, Ashira-personalized (Qwen2.5-Coder-7B + Thai/DevSecOps prompt)
        # Will be upgraded with LoRA adapter after RunPod training.
        RESULT=$(echo "$PROMPT" | "$HOME/.surrogate/bin/surrogate-bridge.sh" 2>>"$LOG")
        ;;
    *)
        echo "[$(date '+%H:%M:%S')] unknown provider $PROVIDER" >> "$LOG"
        exit 1
        ;;
esac

DUR=$(( $(date +%s) - START ))
if [[ -z "$RESULT" ]]; then
    echo "[$(date '+%H:%M:%S')] $PROVIDER failed after ${DUR}s" >> "$LOG"
    # Remove lock so another provider can try
    /bin/rm -f "$LOCK_DIR/${PRIO_ID}_${PROVIDER}" 2>/dev/null
    exit 1
fi

# -------- Save output (same schema as qwen-coder for unified review pipeline) --------
DATE=$(date +%Y-%m-%d_%H-%M)
OUT="$OUT_DIR/${PRIO_ID}_${DATE}.md"

cat > "$OUT" <<EOF
---
priority_id: $PRIO_ID
project: $PRIO_PROJECT
title: $PRIO_TITLE
model: $PROVIDER-cloud
worker: dev-cloud-$PROVIDER
ran_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)
duration_s: $DUR
reviewed: false
---

$RESULT
EOF

echo "[$(date '+%H:%M:%S')] ✅ $PROVIDER → $OUT (${DUR}s, $(wc -c < "$OUT") bytes)" >> "$LOG"
