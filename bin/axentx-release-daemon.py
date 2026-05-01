#!/usr/bin/env python3
"""axentx release — tags semver releases per project every 24h.

Walks each axentx repo, counts commits since last tag, asks LLM to:
- decide bump type (major/minor/patch) based on commit messages
- generate release notes from commit log
Then runs `git tag` + `git push --tags` in repo + creates GitHub Release
via `gh release create`. Conservative — only fires if >=5 commits since
last tag (avoid noisy daily v0.0.X releases)."""
from __future__ import annotations
import os, sys, subprocess, datetime, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from axentx_pipeline import (REPO_ROOT, log, call_llm, daemon_loop)
POLL_SEC = int(os.environ.get("RELEASE_POLL_SEC", "86400"))  # 24h
PROJECTS_ROOT = Path(os.environ.get("AXENTX_ROOT", "/opt/axentx"))
PROJECTS = ["Costinel","vanguard","airship","axiomops","workio","surrogate-1"]

REL_SYSTEM = """Given a list of commit subjects since the last tag, decide
semver bump (major|minor|patch) and write release notes (markdown).
Strict JSON:
{"bump":"patch|minor|major","notes":"<markdown release notes>"}
Rules: ANY breaking change → major. New feature commits → minor. Else patch."""

def git(repo, *args, timeout=20):
    return subprocess.run(["git","-C",str(repo),*args],
                          capture_output=True, text=True, timeout=timeout)

def bump_version(prev, kind):
    try: parts = [int(x) for x in prev.lstrip("v").split(".")]
    except: parts = [0,0,0]
    while len(parts) < 3: parts.append(0)
    if kind == "major":  parts = [parts[0]+1, 0, 0]
    elif kind == "minor": parts = [parts[0], parts[1]+1, 0]
    else:                parts[2] += 1
    return f"v{parts[0]}.{parts[1]}.{parts[2]}"

def do_one() -> bool:
    fired = 0
    for proj in PROJECTS:
        repo = PROJECTS_ROOT / proj
        if not (repo / ".git").exists(): continue
        # last tag
        r = git(repo, "describe", "--tags", "--abbrev=0")
        last_tag = r.stdout.strip() if r.returncode == 0 else "v0.0.0"
        # commits since
        log_range = f"{last_tag}..HEAD" if last_tag else "HEAD"
        r = git(repo, "log", log_range, "--pretty=format:%s")
        subjects = [s for s in r.stdout.splitlines() if s][:50]
        if len(subjects) < 5:
            continue
        log("release", f"▸ {proj}: {len(subjects)} commits since {last_tag}")
        try:
            out = call_llm(
                f"Project: {proj}\nLast tag: {last_tag}\nCommit subjects:\n" +
                "\n".join(f"- {s}" for s in subjects),
                system=REL_SYSTEM, max_tokens=900, timeout=45)
            txt = out.strip()
            if "```" in txt: txt = txt.split("```")[1]
            if txt.startswith("json"): txt = txt[4:]
            d = json.loads(txt.strip())
        except Exception as e:
            log("release", f"  ⚠ {proj} llm fail: {e}"); continue
        new_tag = bump_version(last_tag, d.get("bump","patch"))
        notes_path = repo / f".axentx-release-{new_tag}.md"
        notes_path.write_text(d.get("notes", f"Release {new_tag}\n"))
        # tag + push
        git(repo, "tag", "-a", new_tag, "-m", f"axentx release {new_tag}")
        push = git(repo, "push", "origin", new_tag)
        if push.returncode != 0:
            log("release", f"  ⚠ {proj} push tag fail: {push.stderr[:120]}")
            continue
        # GitHub Release via gh
        try:
            subprocess.run(["gh","release","create",new_tag,"--repo",f"axentx/{proj}",
                          "-F",str(notes_path),"--title",new_tag],
                         check=True, cwd=str(repo), capture_output=True, timeout=30)
            log("release", f"  ✓ {proj} {last_tag} → {new_tag} (gh release created)")
            fired += 1
        except Exception as e:
            log("release", f"  ⚠ {proj} gh release fail: {e}")
    return fired > 0

if __name__ == "__main__":
    daemon_loop("release", POLL_SEC, do_one)
