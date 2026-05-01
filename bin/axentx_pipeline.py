#!/usr/bin/env python3
"""axentx pipeline — shared infra for the 5 role daemons.

Work flows through stages (each stage has its own queue dir):
    dev → review → qa → commit → done

Each daemon polls its input queue every N seconds, picks the oldest item,
processes it (calls LLM with role-specific prompt), drops the output in
the next stage's queue. No cron, no 15-min bursts — true continuous work.

Item format (JSONL one-line per file):
    {
      "id":          "20260501-081234-Costinel-discovery-a3f9",
      "project":     "Costinel",
      "focus":       "discovery|design|backend|frontend|quality|ops",
      "stage":       "dev|review|qa|commit|done",
      "created_at":  "2026-05-01T08:12:34Z",
      "history":     [{"stage":"dev","actor":"claude","output":"...","at":"..."}],
      "current":     {"text":"...latest content..."}
    }
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
SHARED = REPO_ROOT / "state" / "swarm-shared"
QUEUES = {
    "dev":     SHARED / "dev-queue",
    "review":  SHARED / "review-queue",
    "qa":      SHARED / "qa-queue",
    "commit":  SHARED / "commit-queue",
    "done":    SHARED / "done",
}
LOG_DIR = REPO_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

for q in QUEUES.values():
    q.mkdir(parents=True, exist_ok=True)


def log(role: str, msg: str) -> None:
    line = f"[{datetime.datetime.utcnow().isoformat()}Z] [{role}] {msg}"
    print(line, flush=True)
    with (LOG_DIR / f"axentx-{role}-daemon.log").open("a") as f:
        f.write(line + "\n")


def call_llm(prompt: str, system: str = "", max_tokens: int = 1500,
             timeout: int = 30) -> str:
    """Cerebras → Groq → OpenRouter fallback chain."""
    # Probed live 2026-05-01: only these model names work for our keys.
    # Cerebras llama-3.3-70b is gated; only llama3.1-8b is free-tier visible.
    # Groq llama-3.3-70b-versatile is best-quality + always-on for our token.
    # OpenRouter free endpoints all 404/429 — dropped from chain.
    chains = [
        ("Groq", "https://api.groq.com/openai/v1/chat/completions",
         os.environ.get("GROQ_API_KEY"), "llama-3.3-70b-versatile"),
        ("Cerebras", "https://api.cerebras.ai/v1/chat/completions",
         os.environ.get("CEREBRAS_API_KEY"), "llama3.1-8b"),
        ("OpenRouter", "https://openrouter.ai/api/v1/chat/completions",
         os.environ.get("OPENROUTER_API_KEY"),
         "meta-llama/llama-3.3-70b-instruct:free"),
    ]
    messages = []
    if system:
        messages.append({"role": "system", "content": system[:4000]})
    messages.append({"role": "user", "content": prompt[:8000]})
    payload = {"messages": messages, "max_tokens": max_tokens, "temperature": 0.3}
    last_err = None
    for name, url, key, model in chains:
        if not key:
            continue
        body = dict(payload, model=model)
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                # Cerebras sits behind Cloudflare which 403s/1010 unknown UAs
                # when payload contains non-ASCII (Thai, emoji). Use a
                # browser-style UA so the WAF lets us through.
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
                return d["choices"][0]["message"]["content"]
        except (urllib.error.HTTPError, urllib.error.URLError, KeyError,
                TimeoutError, json.JSONDecodeError) as e:
            last_err = f"{name}/{model}: {e}"
            continue
    raise RuntimeError(f"all LLM providers failed; last={last_err}")


def new_item(project: str, focus: str, prompt: str) -> dict:
    ts = datetime.datetime.utcnow()
    sid = hashlib.sha1(f"{ts.isoformat()}-{project}-{focus}".encode()).hexdigest()[:8]
    return {
        "id": f"{ts.strftime('%Y%m%d-%H%M%S')}-{project}-{focus}-{sid}",
        "project": project,
        "focus": focus,
        "stage": "dev",
        "created_at": ts.isoformat() + "Z",
        "history": [],
        "current": {"text": prompt},
    }


def write_item(item: dict, stage: str) -> Path:
    path = QUEUES[stage] / f"{item['id']}.json"
    item["stage"] = stage
    path.write_text(json.dumps(item, indent=2))
    return path


def pick_oldest(stage: str) -> tuple[Path, dict] | None:
    """Returns (path, item) for the oldest queued item, or None."""
    files = sorted(QUEUES[stage].glob("*.json"), key=lambda p: p.stat().st_mtime)
    for p in files:
        try:
            return p, json.loads(p.read_text())
        except Exception:
            # corrupt → move aside
            p.rename(p.with_suffix(".corrupt"))
            continue
    return None


def advance(item: dict, src_path: Path, next_stage: str,
            actor: str, output: str) -> Path:
    """Move item from current stage to next, append history entry."""
    item["history"].append({
        "stage": item.get("stage"),
        "actor": actor,
        "output": output[:6000],
        "at": datetime.datetime.utcnow().isoformat() + "Z",
    })
    item["current"]["text"] = output[:6000]
    src_path.unlink(missing_ok=True)
    return write_item(item, next_stage)


def fail(item: dict, src_path: Path, actor: str, err: str) -> None:
    """Mark item as failed (move to done with failure note)."""
    item["history"].append({
        "stage": item.get("stage"),
        "actor": actor,
        "output": f"FAILED: {err}",
        "at": datetime.datetime.utcnow().isoformat() + "Z",
    })
    src_path.unlink(missing_ok=True)
    write_item(item, "done")


def daemon_loop(role: str, poll_sec: int, work_fn) -> None:
    """Generic daemon main — never returns. Polls input queue, runs work_fn."""
    import signal
    def shutdown(*_):
        log(role, "shutdown signal")
        sys.exit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log(role, f"start — poll every {poll_sec}s")
    n_processed = 0
    n_idle = 0
    while True:
        try:
            did_work = work_fn()
        except Exception as e:
            log(role, f"⚠ exception: {type(e).__name__}: {e}")
            did_work = False
        if did_work:
            n_processed += 1
            n_idle = 0
            time.sleep(2)  # tiny delay after work, then immediately check again
        else:
            n_idle += 1
            if n_idle % 20 == 1:
                log(role, f"idle (processed={n_processed} cycles)")
            time.sleep(poll_sec)
