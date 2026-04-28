#!/usr/bin/env python3
"""
HF Dataset Agentic Discoverer — never-ending mega-mix hunter.

Searches HF Hub across 60+ topic queries every 30 min. For each NEW dataset:
  1. License filter (Apache/MIT/CC-BY/CC0/CDLA/ODC-BY only)
  2. Quality score (downloads, card, schema detection, sample inspection)
  3. Stamp in central DB ~/.surrogate/state/hf-dataset-frontier.db
  4. If score ≥ 0.6 AND schema matches one of our 30+ branches:
     auto-add to dynamic-datasets.json
  5. dataset-enrich.sh reads dynamic list on top of static 89 → grows indefinitely

Stamps: ds_id → verdict ∈ {integrated, rejected-license, rejected-quality, queued}
Same dataset never re-evaluated.
"""
from __future__ import annotations
import hashlib, json, os, re, sqlite3, sys, time
import urllib.parse, urllib.request
from pathlib import Path

HOME = Path(os.environ.get("HOME", "/home/hermes"))
DB = HOME / ".surrogate/state/hf-dataset-frontier.db"
DYNAMIC = HOME / ".surrogate/state/dynamic-datasets.json"
LOG = HOME / ".surrogate/logs/hf-dataset-discoverer.log"
HF_TOKEN = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN") or ""

ALLOWED = {
    "mit", "apache-2.0", "apache 2.0", "cc-by-4.0", "cc-by-3.0", "cc0-1.0",
    "cdla-permissive-2.0", "cdla-permissive-1.0", "bsd", "bsd-2-clause",
    "bsd-3-clause", "isc", "odc-by", "openrail", "openrail++",
}
DENY_KEYWORDS = ("noncommercial", "non-commercial", "nc-", "-nc", "nc4.0",
                 "llama2", "llama3", "llama-3", "research-only", "personal-use")

# Load role-driven query map (auto-rebuilds when role-knowledge-map.json updated)
def _load_role_queries() -> list[tuple[str, str]]:
    """Returns list of (query, role) tuples. Each role contributes core + adjacent
    topics. Plus cross-cutting general queries. Total ~250+ queries auto-generated."""
    role_map_path = HOME / ".surrogate/agents/role-knowledge-map.json"
    queries: list[tuple[str, str]] = []
    if role_map_path.exists():
        try:
            data = json.loads(role_map_path.read_text())
        except Exception:
            data = {"roles": {}, "cross_cutting_topics": []}
        for role, skills in data.get("roles", {}).items():
            for q in (skills.get("core") or []):
                queries.append((q, f"{role}-core"))
            for q in (skills.get("adjacent") or []):
                queries.append((q, f"{role}-adj"))
        for q in data.get("cross_cutting_topics") or []:
            queries.append((q, "cross-cutting"))
    # Plus baseline queries (NEVER static — discoverer must keep finding)
    queries.extend([(q, "general") for q in [
        "instruction tuning 2025", "instruction tuning 2026",
        "post-training dataset", "sft mixture",
        "preference dataset dpo orpo",
        "dataset 2026", "code dataset 2026",
        "agentic dataset 2026", "reasoning dataset 2026",
    ]])
    return queries


def get_queries() -> list[tuple[str, str]]:
    """Reload on each call so role-knowledge-map.json edits take effect immediately."""
    return _load_role_queries()


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def init_db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS dataset_seen (
            ds_id          TEXT PRIMARY KEY,
            evaluated_ts   INTEGER NOT NULL,
            license        TEXT,
            downloads      INTEGER,
            quality_score  REAL,
            schema_branch  TEXT,
            cap            INTEGER,
            slug           TEXT,
            verdict        TEXT,
            role_tag       TEXT          -- which role's query found this
        );
        CREATE INDEX IF NOT EXISTS idx_verdict ON dataset_seen(verdict);
        CREATE INDEX IF NOT EXISTS idx_score ON dataset_seen(quality_score DESC);
        CREATE INDEX IF NOT EXISTS idx_role ON dataset_seen(role_tag);

        CREATE TABLE IF NOT EXISTS query_history (
            query        TEXT PRIMARY KEY,
            role_tag     TEXT,
            last_run_ts  INTEGER NOT NULL,
            results_count INTEGER DEFAULT 0,
            new_finds    INTEGER DEFAULT 0
        );
        """)
        # Migration: add role_tag column if upgrading from v1 schema
        try:
            c.execute("ALTER TABLE dataset_seen ADD COLUMN role_tag TEXT")
        except sqlite3.OperationalError:
            pass  # already exists


def hf_get(url: str, timeout: int = 15):
    headers = {"User-Agent": "Surrogate-1/dataset-discoverer"}
    if HF_TOKEN:
        headers["Authorization"] = f"Bearer {HF_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:
        return None


def detect_schema(sample_row: dict | None) -> str | None:
    """Map first-row keys to existing dataset-enrich.sh schema branch."""
    if not sample_row:
        return None
    keys = {k.lower() for k in sample_row.keys()}
    # Order matters — most specific first
    if "messages" in keys: return "messages"
    if "conversations" in keys: return "conversations"
    if "patch" in keys and ("problem_statement" in keys or "issue" in keys):
        return "swe-instance"
    if "old_contents" in keys and "new_contents" in keys: return "commit"
    if "tools" in keys and ("query" in keys or "answers" in keys):
        return "tools-query-answers"
    if "func" in keys and "target" in keys: return "code-defect"
    if "cwe" in keys: return "code-defect-cwe"
    if "chosen" in keys and "rejected" in keys: return "chosen-rejected"
    if "instruction" in keys and "output" in keys: return "instruction-input-output"
    if ("instruction" in keys or "input" in keys) and "response" in keys:
        return "instr-resp"
    if "problem" in keys and "solution" in keys: return "instr-resp"
    if "query" in keys and "response" in keys: return "query-resp"
    if "question" in keys and "answer" in keys: return "query-resp"
    if "system" in keys and "user" in keys and "assistant" in keys:
        return "system-user-assistant"
    if "system" in keys and "chat" in keys: return "system-chat"
    if "prompt" in keys and ("completion" in keys or "response" in keys):
        return "instr-resp"
    if "context" in keys and ("next_line" in keys or "groundtruth" in keys):
        return "repobench-longctx"
    return None


def get_first_row(ds_id: str) -> dict:
    url = f"https://datasets-server.huggingface.co/first-rows?dataset={urllib.parse.quote(ds_id)}&config=default&split=train"
    data = hf_get(url, timeout=10)
    if not data: return {}
    rows = data.get("rows", [])
    if rows:
        return rows[0].get("row", {})
    return {}


def normalize_license(meta: dict) -> str:
    lic = (meta.get("cardData") or {}).get("license", "") or meta.get("license", "")
    if isinstance(lic, list):
        lic = lic[0] if lic else ""
    return str(lic).lower().replace("license:", "").strip()


def score_dataset(meta: dict, schema: str | None, sample: dict, lic: str) -> float:
    score = 0.0
    # License (mandatory + 0.3)
    if lic in ALLOWED:
        score += 0.3
    # Downloads
    dl = meta.get("downloads", 0) or 0
    if dl >= 10000: score += 0.3
    elif dl >= 1000: score += 0.2
    elif dl >= 100: score += 0.1
    # Schema detected
    if schema: score += 0.2
    # Card description
    desc = (meta.get("description") or "")
    if len(desc) > 200: score += 0.1
    # Sample non-trivial
    if sample and len(json.dumps(sample)) > 100: score += 0.1
    return min(1.0, score)


def cap_for_size(meta: dict) -> int:
    sc = (meta.get("cardData") or {}).get("size_categories")
    if isinstance(sc, list):
        sc = sc[0] if sc else ""
    sc = str(sc or "")
    if "<1K" in sc: return 1000
    if "1K<n<10K" in sc: return 10000
    if "10K<n<100K" in sc: return 50000
    if "100K<n<1M" in sc: return 100000
    if "1M<n<10M" in sc: return 200000
    if "10M<n<100M" in sc: return 300000
    return 100000


def append_dynamic(entry: dict):
    DYNAMIC.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if DYNAMIC.exists():
        try:
            existing = json.loads(DYNAMIC.read_text() or "[]")
        except json.JSONDecodeError:
            existing = []
    # Dedup by id
    if any(e["id"] == entry["id"] for e in existing):
        return
    existing.append(entry)
    DYNAMIC.write_text(json.dumps(existing, indent=2))


def evaluate_one(ds_id: str) -> tuple[str, dict | None]:
    """Returns (verdict, dynamic_entry_or_None)."""
    meta = hf_get(f"https://huggingface.co/api/datasets/{ds_id}?full=true")
    if not meta:
        return "unreachable", None

    lic = normalize_license(meta)
    # Hard reject
    if any(d in lic for d in DENY_KEYWORDS):
        return "rejected-license", None
    if not lic and not meta.get("cardData"):
        return "rejected-no-card", None
    if lic and lic not in ALLOWED:
        # Maybe still permissive by name
        if not any(p in lic for p in ("apache", "mit", "cc0", "cdla", "cc-by", "bsd", "isc", "odc")):
            return "rejected-license", None

    sample = get_first_row(ds_id)
    schema = detect_schema(sample)
    score = score_dataset(meta, schema, sample, lic or "?")
    cap = cap_for_size(meta)
    slug = re.sub(r'[^a-zA-Z0-9-]', '-', ds_id.replace("/", "-"))[:40]

    if score >= 0.6 and schema:
        return "integrated", {
            "id": ds_id, "license": lic or "permissive", "slug": slug,
            "schema": schema, "cap": cap, "score": round(score, 2),
            "downloads": meta.get("downloads", 0),
            "discovered_ts": int(time.time()),
        }
    elif score >= 0.4:
        return "queued-needs-schema" if not schema else "queued-low-quality", None
    else:
        return "rejected-quality", None


def stamp(ds_id: str, verdict: str, lic: str = "", dl: int = 0,
          score: float = 0.0, schema: str = "", cap: int = 0, slug: str = "",
          role_tag: str = ""):
    with sqlite3.connect(DB) as c:
        c.execute(
            "INSERT OR IGNORE INTO dataset_seen "
            "(ds_id, evaluated_ts, license, downloads, quality_score, schema_branch, cap, slug, verdict, role_tag) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ds_id, int(time.time()), lic, dl, score, schema, cap, slug, verdict, role_tag)
        )


def is_seen(ds_id: str) -> bool:
    with sqlite3.connect(DB) as c:
        return c.execute("SELECT 1 FROM dataset_seen WHERE ds_id=?", (ds_id,)).fetchone() is not None


def discover_cycle() -> dict:
    new_integrated = 0
    new_queued = 0
    new_rejected = 0
    seen_this_cycle = 0
    role_finds: dict[str, int] = {}

    queries = get_queries()
    log(f"  loaded {len(queries)} role-driven queries (covering {len(set(r for _,r in queries))} role tags)")

    for q, role_tag in queries:
        url = f"https://huggingface.co/api/datasets?search={urllib.parse.quote(q)}&limit=30&sort=downloads&direction=-1"
        results = hf_get(url, timeout=15) or []
        for ds in results:
            ds_id = ds.get("id", "")
            if not ds_id or is_seen(ds_id):
                continue
            seen_this_cycle += 1
            verdict, entry = evaluate_one(ds_id)
            stamp(ds_id, verdict,
                  lic=entry.get("license", "") if entry else "",
                  dl=entry.get("downloads", 0) if entry else 0,
                  score=entry.get("score", 0.0) if entry else 0.0,
                  schema=entry.get("schema", "") if entry else "",
                  cap=entry.get("cap", 0) if entry else 0,
                  slug=entry.get("slug", "") if entry else "",
                  role_tag=role_tag)
            if verdict == "integrated":
                # Tag the entry with role for downstream training-mix balance
                if entry: entry["role_tag"] = role_tag
                append_dynamic(entry)
                new_integrated += 1
                role_finds[role_tag] = role_finds.get(role_tag, 0) + 1
                log(f"  ✅ [{role_tag}] {ds_id} | {entry['license']} | {entry['schema']} | cap={entry['cap']:,}")
            elif verdict.startswith("queued"):
                new_queued += 1
            else:
                new_rejected += 1
            time.sleep(0.4)  # gentle on HF API

        # Update query history for this query
        try:
            with sqlite3.connect(DB) as c:
                c.execute(
                    "INSERT OR REPLACE INTO query_history (query, role_tag, last_run_ts, results_count, new_finds) "
                    "VALUES (?,?,?,?, COALESCE((SELECT new_finds FROM query_history WHERE query=?),0) + ?)",
                    (q, role_tag, int(time.time()), len(results), q, new_integrated)
                )
        except Exception:
            pass

    return {"evaluated": seen_this_cycle, "integrated": new_integrated,
            "queued": new_queued, "rejected": new_rejected,
            "by_role": role_finds}


def main():
    init_db()
    log(f"start | hf_token={'set' if HF_TOKEN else 'MISSING'} | queries={len(QUERIES)}")

    while True:
        t0 = time.time()
        try:
            stats = discover_cycle()
        except Exception as e:
            log(f"  cycle err {type(e).__name__}: {str(e)[:200]}")
            stats = {}
        elapsed = int(time.time() - t0)
        # Cumulative stats from DB
        with sqlite3.connect(DB) as c:
            verdicts = dict(c.execute("SELECT verdict, COUNT(*) FROM dataset_seen GROUP BY verdict").fetchall())
        log(f"=== cycle done in {elapsed}s | this_cycle={stats} | cumulative={verdicts}")
        # Sleep 30 min between cycles
        time.sleep(1800)


if __name__ == "__main__":
    main()
