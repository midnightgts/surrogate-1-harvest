#!/usr/bin/env python3
"""axentx BD daemon — opportunity classifier.

Consumes pain-point reports from research-queue. For each pain, asks the
LLM to classify it against the active axentx product portfolio:

  Costinel  — AWS cost analytics + anomaly detection
  vanguard  — security / compliance posture
  airship   — IaC / cloud platform deployment
  workio    — workflow automation
  axiomops  — DevSecOps tooling
  surrogate — Surrogate-1 (this entire stack — autonomous AI dev agent)

Verdict: either
  EXTEND <project>  → pain is best solved as a feature on an existing
                      project. Item proceeds to design-queue with the
                      target project tagged.
  NEW-PRODUCT       → pain demands a fresh product. Item proceeds to
                      design-queue with project=null; the design-thinking
                      daemon will validate fit before BMC.
  PASS              → pain is real but not strategic for axentx (e.g.
                      consumer apps, gaming, hardware). Marked done
                      with reason. Saves cycles downstream.

Note: BD does NOT decide on funding/build/ship. It only triages signal
quality and routes — design-thinking + business + marketing daemons
each contribute their lens before any code path is started.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from axentx_pipeline import (REPO_ROOT, log, call_llm, pick_oldest, advance,
                             fail, daemon_loop)

POLL_SEC = int(os.environ.get("BD_POLL_SEC", "60"))


PORTFOLIO = """Active axentx product portfolio (decide if pain fits one):

1. Costinel  — AWS cost analytics + anomaly detection. SREs/finops who burn
               cloud spend on misconfigured infra. Solves: surprise bills,
               unused resources, forecasting.
2. vanguard  — Cloud security posture management (CSPM). Compliance officers,
               solo devs who need SOC2-lite. Solves: misconfigured S3/IAM,
               drift detection, audit evidence.
3. airship   — IaC / multi-cloud deployment platform. Devs shipping to AWS+GCP+
               Cloudflare without knowing 3 stacks. Solves: deploy-once-target-
               many, env parity.
4. workio    — Workflow automation (think Zapier for engineering teams).
               Solves: glue between GitHub/Slack/Jira/HF without writing scripts.
5. axiomops  — DevSecOps integrated tooling. Senior platform engineers.
               Solves: CI/CD + obs + on-call in one stack instead of 6 vendors.
6. surrogate — Surrogate-1: autonomous AI dev agent (this stack). Devs who
               want commits/reviews/tests/docs done while they sleep, on
               cloud free tier."""

BD_SYSTEM = (
    "You are a Head of BD doing portfolio triage. For each user pain point, "
    "decide which axentx product it fits — or whether it deserves a new "
    "product, or whether to pass entirely.\n\n"
    f"{PORTFOLIO}\n\n"
    "Output STRICT JSON:\n\n"
    "{\n"
    '  "verdict": "EXTEND|NEW-PRODUCT|PASS",\n'
    '  "target_project": "Costinel|vanguard|airship|workio|axiomops|surrogate|null",\n'
    '  "rationale": "1-2 sentences why this fit or why pass",\n'
    '  "feature_one_liner": "if EXTEND: the feature in one sentence",\n'
    '  "new_product_one_liner": "if NEW-PRODUCT: the product hypothesis in one sentence",\n'
    '  "tam_signal": "low|medium|high — how broad is the affected audience",\n'
    '  "axentx_advantage": "why we can win this vs a generic competitor (1 sentence)"\n'
    "}\n\n"
    "Rules:\n"
    "- EXTEND wins ties — adding a feature to an existing product is 10× cheaper than starting new.\n"
    "- NEW-PRODUCT only when the pain is in a domain we have NO product for AND has high TAM.\n"
    "- PASS for: consumer/gaming/hardware/non-technical pains, or pains where the audience is too narrow.\n"
    "- Be specific about feature_one_liner / new_product_one_liner — NO 'AI-powered platform' fluff."
)


def do_one_bd() -> bool:
    picked = pick_oldest("bd")
    if not picked: return False
    src_path, item = picked
    pain = item.get("verdict", {}) or {}
    post = item.get("post", {}) or {}

    log("bd", f"▸ {item['id'][:30]}  pain: {pain.get('pain_one_liner','')[:60]}")
    prompt = (
        f"Pain summary: {pain.get('pain_one_liner','?')}\n"
        f"Domain: {pain.get('domain','?')}\n"
        f"Audience: {pain.get('audience','?')}\n"
        f"Severity: {pain.get('severity','?')}/10\n"
        f"Source: {post.get('source','?')} ({post.get('score',0)} score, "
        f"{post.get('num_comments',0)} comments)\n"
        f"Evidence quote: {pain.get('evidence','')[:300]}\n\n"
        f"Your verdict (strict JSON only):"
    )
    try:
        out = call_llm(prompt, system=BD_SYSTEM, max_tokens=500, timeout=35)
        txt = out.strip()
        if "```" in txt:
            txt = txt.split("```")[1]
            if txt.startswith("json"): txt = txt[4:]
        verdict = json.loads(txt.strip())
    except Exception as e:
        fail(item, src_path, "bd", f"LLM/parse failed: {e}")
        log("bd", f"✗ {item['id']}: parse fail")
        return True

    item["bd_verdict"] = verdict
    item["history"].append({
        "stage": "bd",
        "actor": "axentx-bd",
        "output": json.dumps(verdict, ensure_ascii=False),
        "at": datetime.datetime.utcnow().isoformat() + "Z",
    })
    item["current"]["text"] = json.dumps(verdict, ensure_ascii=False)

    v = (verdict.get("verdict") or "").upper()
    if v == "PASS":
        # End of road — record decision but stop here
        advance(item, src_path, "done", "bd",
                f"BD-PASS: {verdict.get('rationale','')[:200]}")
        log("bd", f"  ↓ PASS — {verdict.get('rationale','')[:60]}")
    elif v in ("EXTEND", "NEW-PRODUCT"):
        # Forward to design-thinking validator
        item["target_project"] = verdict.get("target_project")
        advance(item, src_path, "design", "bd", json.dumps(verdict))
        log("bd", f"  ✓ {v} → {verdict.get('target_project','new')} → design-queue")
    else:
        # Ambiguous — let design have a look
        advance(item, src_path, "design", "bd", out)
        log("bd", f"  ~ ambiguous → design")
    return True


if __name__ == "__main__":
    daemon_loop("bd", POLL_SEC, do_one_bd)
