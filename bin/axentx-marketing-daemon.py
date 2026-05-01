#!/usr/bin/env python3
"""axentx marketing daemon — positioning + competitor scan + go-to-market.

Pulls items from marketing-queue (where business verdict was BUILD).
Drafts:
  1. positioning statement (Geoffrey Moore template)
  2. competitor map with our differentiators
  3. launch GTM (channel + ICP + first-7-days plan)
  4. messaging pillars + 1 sample landing-page hero copy

Output advances to prd-queue where the PRD daemon turns this into actual
user stories + tasks the dev pipeline can implement.
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

POLL_SEC = int(os.environ.get("MARKETING_POLL_SEC", "90"))


MARKETING_SYSTEM = """You are head of product marketing. For each validated
BUILD opportunity (BMC + market sizing already done), output strict JSON:

{
  "positioning": "For <target customer> who <problem>, <product> is a <category> that <key benefit>. Unlike <main alternative>, <our differentiator>.",
  "icp": {
    "company_size": "solo|smb|midmarket|enterprise",
    "role": "<exact title that holds budget>",
    "trigger": "<the moment they realize they need us>"
  },
  "competitors": [
    {"name": "<vendor>", "positioning": "<how they sell>", "weakness_we_exploit": "<gap we close>"}
  ],
  "messaging_pillars": ["<3 short messages we'd hammer everywhere>"],
  "landing_hero": "<one headline + one subheadline that would convert the ICP>",
  "channels": ["<2-3 channels ranked by likely fit>"],
  "first_7_days_plan": ["<concrete launch tasks day-by-day>"],
  "launch_metric": "<the single metric we measure launch success on day 7 / day 30>"
}

Be ruthlessly specific about ICP — 'developers' is not enough; 'platform
engineers at 50-200 person SaaS companies who use Terraform' is. Channels
must be *cheap* (we're cloud-free-tier — no paid ads day 1). Lean toward:
GitHub README/ Show HN / Product Hunt / dev.to / Indie Hackers / specific
subreddits / cold DM to specific roles."""


def do_one_marketing() -> bool:
    picked = pick_oldest("marketing")
    if not picked: return False
    src_path, item = picked

    pain = item.get("verdict", {}) or {}
    bd = item.get("bd_verdict", {}) or {}
    design = item.get("design_verdict", {}) or {}
    biz = item.get("business_verdict", {}) or {}

    log("marketing", f"▸ {item['id'][:30]}  → {bd.get('target_project','new')}")
    prompt = (
        f"=== context ===\n"
        f"Pain: {pain.get('pain_one_liner','?')}\n"
        f"BD: {bd.get('verdict','?')} → {bd.get('target_project','?')}\n"
        f"Hypothesis: {bd.get('feature_one_liner') or bd.get('new_product_one_liner','')}\n"
        f"Root cause: {design.get('root_cause','?')}\n"
        f"JTBD: {design.get('jtbd','?')}\n"
        f"BMC value props: {(biz.get('bmc') or {}).get('value_propositions','?')}\n"
        f"Pricing: {biz.get('pricing','?')}\n"
        f"NSM: {biz.get('north_star_metric','?')}\n\n"
        f"Draft positioning + ICP + competitor map + GTM + messaging. Strict JSON."
    )
    try:
        out = call_llm(prompt, system=MARKETING_SYSTEM, max_tokens=1800, timeout=60)
        txt = out.strip()
        if "```" in txt:
            txt = txt.split("```")[1]
            if txt.startswith("json"): txt = txt[4:]
        gtm = json.loads(txt.strip())
    except Exception as e:
        fail(item, src_path, "marketing", f"LLM/parse failed: {e}")
        log("marketing", f"✗ {item['id']}: parse fail")
        return True

    item["marketing_verdict"] = gtm
    item["history"].append({
        "stage": "marketing",
        "actor": "axentx-marketing",
        "output": json.dumps(gtm, ensure_ascii=False),
        "at": datetime.datetime.utcnow().isoformat() + "Z",
    })
    item["current"]["text"] = json.dumps(gtm, ensure_ascii=False)

    advance(item, src_path, "prd", "marketing",
            json.dumps(gtm, ensure_ascii=False))
    log("marketing", f"  ✓ → prd-queue (positioning: {gtm.get('positioning','')[:60]})")
    return True


if __name__ == "__main__":
    daemon_loop("marketing", POLL_SEC, do_one_marketing)
