#!/usr/bin/env python3
"""axentx research daemon — mines real-world pain points.

Sources (no API keys needed for read):
  - Reddit JSON (/r/SaaS, /r/Entrepreneur, /r/codingbusiness, /r/devops, /r/startups, /r/freelance, /r/ChatGPTCoding)
  - Hacker News /show + /ask /top
  - dev.to API
  - Indie Hackers public posts (RSS)

For every interesting post (sort=hot, top last 24h), pull title + selftext
and ask the LLM to: extract the underlying pain point in 1-2 sentences,
classify domain, score severity 0-10. Output a research-report record
into the `research-queue` for the BD daemon to triage.

Multiple instances of this daemon can run side-by-side (RESEARCH_WORKER_ID
env distinguishes them); each picks a different subreddit/source on each
cycle so coverage scales linearly with worker count.
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

sys.path.insert(0, str(Path(__file__).parent))
from axentx_pipeline import (REPO_ROOT, log, call_llm, write_item, daemon_loop)

WORKER_ID = os.environ.get("RESEARCH_WORKER_ID", "1")
POLL_SEC = int(os.environ.get("RESEARCH_POLL_SEC", "600"))  # 10 min/cycle
SOURCES_PER_CYCLE = int(os.environ.get("RESEARCH_SOURCES_PER_CYCLE", "3"))

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Subreddits surface real pain — we filter for "actually a problem" via the LLM.
SUBREDDITS = [
    "SaaS", "Entrepreneur", "codingbusiness", "devops", "startups",
    "freelance", "ChatGPTCoding", "selfhosted", "kubernetes", "cybersecurity",
    "MachineLearning", "OpenAI", "ProductManagement", "ExperiencedDevs",
]

CURSOR_FILE = REPO_ROOT / "state" / f"axentx-research-cursor-{WORKER_ID}.json"


RESEARCH_SYSTEM = """You are a market researcher mining real-world pain points
from internet posts. For each post, output strict JSON:

{
  "is_real_pain": true|false,
  "pain_one_liner": "the specific problem in 1 sentence",
  "domain": "saas|devops|security|ml|productivity|finance|education|other",
  "severity": 1-10,
  "audience": "who suffers from this (1 phrase)",
  "evidence": "quote a sentence from the post that proves it's real",
  "search_for_dupes": "3 short search queries to validate this is a recurring pain"
}

REJECT (set is_real_pain=false) if the post is:
- self-promotion / launch / "look what I built"
- venting without a specific problem
- already-solved questions where good answers exist in comments
- pure opinion / discussion / news commentary

ACCEPT only if the post describes a CONCRETE problem the author wants solved
and that affects more than just them. Severity scales with: how often does
this hurt, how much money/time it wastes, how many people share it.\nGROUNDING: Cite at least one concrete source for every claim (URL from the post, dataset/repo name, established framework name, published number). If you cannot cite, say "unverified — needs research" instead of fabricating a number, market size, competitor name, or feature claim. Made-up references are worse than honest gaps.
"""


def fetch_reddit(sub: str) -> list[dict]:
    """Reddit blocks datacenter UA on www; old.reddit + RSS often slips through.
    Try in order: old.reddit JSON → RSS feed. Fallback empty on full block."""
    headers = {
        "User-Agent": f"Surrogate1Bot/0.1 by axentx (worker-{WORKER_ID})",
        "Accept": "application/json",
    }
    posts = []
    for url in (f"https://old.reddit.com/r/{sub}/hot.json?limit=15&t=day",
                f"https://www.reddit.com/r/{sub}/hot.json?limit=15&t=day"):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read())
            for c in (d.get("data") or {}).get("children", [])[:15]:
                p = c.get("data") or {}
                if p.get("stickied") or (p.get("score", 0) < 5):
                    continue
                body = (p.get("selftext") or "").strip()
                if len(body) < 80: continue
                posts.append({
                    "source": f"reddit/r/{sub}",
                    "url": "https://reddit.com" + p.get("permalink", ""),
                    "title": p.get("title", "")[:300],
                    "body": body[:3000],
                    "score": p.get("score", 0),
                    "num_comments": p.get("num_comments", 0),
                })
            return posts
        except urllib.error.HTTPError as e:
            if e.code in (403, 429): continue  # try next URL
            return posts
        except Exception:
            return posts
    return posts


def fetch_lobsters() -> list[dict]:
    """Lobsters — engineering-focused HN alternative. JSON API, no auth."""
    req = urllib.request.Request(
        "https://lobste.rs/hottest.json",
        headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=12) as r:
        items = json.loads(r.read())
    posts = []
    for p in items[:20]:
        body = (p.get("description_plain") or p.get("description") or "").strip()
        if len(body) < 80: continue
        posts.append({
            "source": "lobsters",
            "url": p.get("url") or p.get("short_id_url",""),
            "title": p.get("title","")[:300],
            "body": body[:3000],
            "score": p.get("score", 0),
            "num_comments": len(p.get("comments") or []),
        })
    return posts


def fetch_indiehackers() -> list[dict]:
    """Indie Hackers via RSS — entrepreneurs talk about real pains."""
    import xml.etree.ElementTree as ET
    req = urllib.request.Request(
        "https://www.indiehackers.com/feed.xml",
        headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            xml = r.read()
        root = ET.fromstring(xml)
    except Exception:
        return []
    posts = []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in (root.findall(".//item") or root.findall(".//atom:entry", ns))[:15]:
        title = (entry.findtext("title") or
                 entry.findtext("atom:title", default="", namespaces=ns) or "")
        link = (entry.findtext("link") or
                (entry.find("atom:link", ns).get("href") if entry.find("atom:link", ns) is not None else ""))
        body = (entry.findtext("description") or
                entry.findtext("atom:summary", default="", namespaces=ns) or "")
        if len(body) < 80: continue
        posts.append({
            "source": "indiehackers",
            "url": link, "title": title[:300],
            "body": body[:3000], "score": 0, "num_comments": 0,
        })
    return posts


def fetch_hn() -> list[dict]:
    """Hacker News — top stories of last day."""
    ids_url = "https://hacker-news.firebaseio.com/v0/topstories.json"
    req = urllib.request.Request(ids_url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=10) as r:
        ids = json.loads(r.read())[:30]
    posts = []
    for sid in ids[:15]:
        try:
            req = urllib.request.Request(
                f"https://hacker-news.firebaseio.com/v0/item/{sid}.json",
                headers={"User-Agent": UA},
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                p = json.loads(r.read())
            text = (p.get("text") or "").strip() or p.get("title", "")
            if not text or len(text) < 50:
                continue
            posts.append({
                "source": "hn",
                "url": f"https://news.ycombinator.com/item?id={sid}",
                "title": p.get("title", "")[:300],
                "body": text[:3000],
                "score": p.get("score", 0),
                "num_comments": p.get("descendants", 0),
            })
        except Exception:
            continue
    return posts


def fetch_devto() -> list[dict]:
    url = "https://dev.to/api/articles/latest?per_page=15"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=12) as r:
        data = json.loads(r.read())
    posts = []
    for p in data[:15]:
        body = (p.get("description") or "").strip()
        if len(body) < 80:
            continue
        posts.append({
            "source": "devto",
            "url": p.get("url", ""),
            "title": p.get("title", "")[:300],
            "body": body[:3000],
            "score": p.get("public_reactions_count", 0),
            "num_comments": p.get("comments_count", 0),
        })
    return posts


# Non-Reddit sources FIRST so workers produce signal even when Reddit's
# anti-scrape blocks our datacenter IPs (which it does, hard, on www and
# old.reddit alike). Reddit serves as a long tail — we still try it but
# the pipeline doesn't depend on it.
SOURCES = (
    [("hn", fetch_hn), ("devto", fetch_devto),
     ("lobsters", fetch_lobsters), ("indiehackers", fetch_indiehackers)]
    + [(f"reddit:{s}", lambda s=s: fetch_reddit(s)) for s in SUBREDDITS]
)


def load_cursor() -> dict:
    if CURSOR_FILE.exists():
        try: return json.loads(CURSOR_FILE.read_text())
        except: pass
    # Stagger workers so 3 instances cover 3 different sources at the same time
    return {"src_idx": (int(WORKER_ID) - 1) % len(SOURCES), "seen": []}


def save_cursor(c: dict) -> None:
    CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Cap seen-list to last 5000 to stop unbounded growth
    c["seen"] = c.get("seen", [])[-5000:]
    CURSOR_FILE.write_text(json.dumps(c, indent=2))


def post_fingerprint(post: dict) -> str:
    return hashlib.sha256(
        (post.get("source", "") + "|" + post.get("url", "")).encode()
    ).hexdigest()[:16]


def do_one_cycle() -> bool:
    c = load_cursor()
    seen = set(c.get("seen", []))
    fired = 0

    for _ in range(SOURCES_PER_CYCLE):
        idx = c["src_idx"] % len(SOURCES)
        name, fetcher = SOURCES[idx]
        c["src_idx"] += 1
        log(f"research-{WORKER_ID}", f"▸ pulling {name}")
        try:
            posts = fetcher()
        except Exception as e:
            log(f"research-{WORKER_ID}", f"  ✗ {name}: {type(e).__name__}: {str(e)[:80]}")
            time.sleep(2)
            continue

        for post in posts:
            fp = post_fingerprint(post)
            if fp in seen:
                continue
            seen.add(fp)

            # Ask LLM if this is a real pain point worth chasing
            prompt = (
                f"Source: {post['source']}\n"
                f"Title: {post['title']}\n"
                f"Score: {post['score']} | Comments: {post['num_comments']}\n"
                f"URL: {post['url']}\n\n"
                f"Body:\n{post['body']}\n\n"
                f"Output strict JSON only — no commentary."
            )
            try:
                out = call_llm(prompt, system=RESEARCH_SYSTEM,
                               max_tokens=400, timeout=30)
                # Extract JSON (LLM sometimes wraps in code fences)
                txt = out.strip()
                if "```" in txt:
                    txt = txt.split("```")[1]
                    if txt.startswith("json"):
                        txt = txt[4:]
                verdict = json.loads(txt.strip())
            except Exception as e:
                log(f"research-{WORKER_ID}", f"  ⚠ LLM/parse fail on {post['url'][:60]}: {str(e)[:60]}")
                continue

            if not verdict.get("is_real_pain"):
                continue
            if verdict.get("severity", 0) < 5:
                continue  # noise filter

            # Push to research-queue for BD daemon
            ts = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
            item_id = f"{ts}-pain-{fp}"
            item = {
                "id": item_id,
                "stage": "research",
                "created_at": datetime.datetime.utcnow().isoformat() + "Z",
                "post": post,
                "verdict": verdict,
                "history": [{
                    "stage": "research",
                    "actor": f"axentx-research-{WORKER_ID}",
                    "output": json.dumps(verdict, ensure_ascii=False),
                    "at": datetime.datetime.utcnow().isoformat() + "Z",
                }],
                "current": {"text": json.dumps(verdict, ensure_ascii=False)},
            }
            write_item(item, "bd")  # next stage = BD triage
            log(f"research-{WORKER_ID}",
                f"  ✓ pain (sev {verdict.get('severity')}): {verdict.get('pain_one_liner','')[:80]}")
            fired += 1

    c["seen"] = list(seen)
    save_cursor(c)
    log(f"research-{WORKER_ID}", f"cycle done — {fired} new pain items pushed → bd-queue")
    return fired > 0


if __name__ == "__main__":
    daemon_loop(f"research-{WORKER_ID}", POLL_SEC, do_one_cycle)
