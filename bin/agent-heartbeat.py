"""agent-heartbeat — tiny library every daemon imports to emit status.

Purpose (user directive 2026-05-02):
  > "แล้วจะรู้ว่า agent ไหนทำงานตอนไหน"
  > "ดู agent ทั้งหมด แล้วดูว่าใครต้องทำงานเมื่อไหร่ตอนไหน"

How it works:
  Each daemon calls heartbeat("research-1", state="working", task="reddit:devops")
  on every cycle. That writes a single key to Cloudflare KV (namespace
  HEARTBEAT_KV) with:
    {agent, host, pid, state, task, last_seen, cycle_n, ...}
  TTL 5 min — stale rows auto-expire so dead daemons drop off the dashboard.

  The CF Worker exposes /dash/agents which fans out KV.list() and renders
  a live grid of every reporting agent.

Why CF KV (not Supabase):
  KV is HTTPS-reachable from any VM (no IPv6/pooler issues), reads from
  the worker's own runtime are zero-latency, and the free tier is plenty
  for ~30 agents × heartbeat-every-60s = ~43k writes/day (well under 1M).

Design notes:
  - Fail-silent — heartbeat MUST NOT break the agent. Network blip → skip
    one tick, retry next cycle.
  - Background thread — heartbeat runs every HEARTBEAT_SEC in the
    background after start_heartbeat() so the agent's main loop is never
    blocked on a CF API call.
"""
from __future__ import annotations

import datetime
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request

CF_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")
CF_ACCT = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
KV_ID = os.environ.get("HEARTBEAT_KV_ID", "")  # CF namespace ID
HEARTBEAT_SEC = int(os.environ.get("HEARTBEAT_SEC", "60"))
HEARTBEAT_TTL = int(os.environ.get("HEARTBEAT_TTL", "300"))  # 5 min
HOSTNAME = socket.gethostname()

_state: dict = {
    "agent": "",
    "host": HOSTNAME,
    "pid": os.getpid(),
    "state": "starting",     # starting|idle|working|error|shutting-down
    "task": "",              # short label of current work
    "cycle_n": 0,
    "last_error": "",
    "started_at": datetime.datetime.utcnow().isoformat() + "Z",
    "last_seen": "",
}
_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop_evt = threading.Event()


def _kv_put(key: str, value: dict) -> None:
    """Write to CF KV with TTL. Best-effort, swallow all errors."""
    if not (CF_TOKEN and CF_ACCT and KV_ID):
        return
    url = (f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCT}"
           f"/storage/kv/namespaces/{KV_ID}/values/{key}"
           f"?expiration_ttl={HEARTBEAT_TTL}")
    body = json.dumps(value).encode()
    req = urllib.request.Request(url, data=body, method="PUT", headers={
        "Authorization": f"Bearer {CF_TOKEN}",
        "Content-Type": "application/json",
    })
    try:
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass  # heartbeat is best-effort by design


def heartbeat(agent: str, *, state: str = "working", task: str = "",
              error: str | None = None, cycle_n: int | None = None) -> None:
    """Update local state. Background thread will flush to KV on next tick."""
    with _lock:
        _state["agent"] = agent
        _state["state"] = state
        if task:
            _state["task"] = task[:200]
        if error is not None:
            _state["last_error"] = str(error)[:300]
        if cycle_n is not None:
            _state["cycle_n"] = cycle_n


def _flush_loop() -> None:
    while not _stop_evt.is_set():
        with _lock:
            _state["last_seen"] = datetime.datetime.utcnow().isoformat() + "Z"
            agent = _state["agent"]
            snap = dict(_state)
        if agent:
            _kv_put(f"agent:{agent}", snap)
        # Sleep in small slices so SIGTERM exits cleanly within ~1s
        for _ in range(HEARTBEAT_SEC):
            if _stop_evt.is_set():
                break
            time.sleep(1)


def start_heartbeat(agent: str, initial_state: str = "starting") -> None:
    """Call once at daemon startup. Idempotent."""
    global _thread
    heartbeat(agent, state=initial_state, task="boot")
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_flush_loop, daemon=True,
                               name=f"heartbeat-{agent}")
    _thread.start()


def stop_heartbeat() -> None:
    """Call from SIGTERM/SIGINT handler. Final 'shutting-down' write."""
    with _lock:
        _state["state"] = "shutting-down"
        _state["last_seen"] = datetime.datetime.utcnow().isoformat() + "Z"
        snap = dict(_state)
        agent = _state["agent"]
    if agent:
        _kv_put(f"agent:{agent}", snap)
    _stop_evt.set()
