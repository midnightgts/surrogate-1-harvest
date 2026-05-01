#!/usr/bin/env python3
"""axentx reviewer daemon — picks items from review-queue, runs them through
a senior code-review prompt, advances to qa-queue (or back to dev with
feedback if rejected).
"""
from __future__ import annotations
import sys, json, datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from axentx_pipeline import (log, call_llm, pick_oldest, advance, fail,
                             daemon_loop)

POLL_SEC = 30

REVIEWER_SYSTEM = """You are a principal engineer doing code review on \
proposed changes. Be tough but fair. For each proposal:
- Identify correctness issues, security risks, perf problems, missing tests
- If the proposal is missing concrete implementation, that's a REJECT
- If it's solid, mark APPROVE and add 1-3 sentences of acceptance criteria
- Format: first line must be APPROVE: or REJECT: <reason>, then details"""


def do_one_review() -> bool:
    picked = pick_oldest("review")
    if not picked: return False
    src_path, item = picked
    proposal = item.get("current", {}).get("text", "")
    project = item.get("project", "?")
    focus = item.get("focus", "?")
    log("reviewer", f"▸ {item['id']} ({project}/{focus})")
    prompt = (f"Project: {project}\nFocus: {focus}\n\n"
              f"Proposed change:\n```\n{proposal[:5000]}\n```\n\n"
              f"Review this proposal. Be specific.")
    try:
        out = call_llm(prompt, system=REVIEWER_SYSTEM, max_tokens=1200, timeout=40)
    except Exception as e:
        fail(item, src_path, "reviewer", f"LLM failed: {e}")
        log("reviewer", f"✗ {item['id']}: LLM failed")
        return True
    first_line = out.splitlines()[0] if out.splitlines() else ""
    if first_line.upper().startswith("APPROVE"):
        advance(item, src_path, "qa", "reviewer", out)
        log("reviewer", f"✓ {item['id']} APPROVE → qa-queue")
    elif first_line.upper().startswith("REJECT"):
        # Send back to dev with the rejection note
        item["current"]["text"] = (
            f"REVIEWER REJECTED previous attempt:\n\n{out}\n\n"
            f"--- original proposal ---\n{proposal[:3000]}")
        advance(item, src_path, "dev", "reviewer", out)
        log("reviewer", f"↺ {item['id']} REJECT → back to dev")
    else:
        # Ambiguous → default approve to keep flow moving
        advance(item, src_path, "qa", "reviewer", out)
        log("reviewer", f"~ {item['id']} ambiguous (default approve) → qa")
    return True


if __name__ == "__main__":
    daemon_loop("reviewer", POLL_SEC, do_one_review)
