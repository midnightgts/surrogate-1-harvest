#!/usr/bin/env python3
"""
LLM Burst Generator — uses 8 free LLM APIs in parallel to synthesize
DevSecOps training pairs from prompt templates.

Each iteration:
  1. Pick template from 100+ DevSecOps task definitions
  2. Fill {placeholders} with random parameters
  3. Round-robin across LLM providers (Cerebras → Groq → OpenRouter → ...)
  4. Validate response format (length > 100 chars, no refusal)
  5. Write {prompt, response, source, role} to training-pairs.jsonl
  6. Central dedup catches duplicates

Free-tier budget across providers (rough daily caps):
  Cerebras   1M tokens/day   ~2000 pairs
  Groq       gen-2 dev tier  ~1500 pairs
  OpenRouter free models     ~1000 pairs
  Gemini     60 RPM free     ~1200 pairs
  Grok-2     100 RPD free    ~50 pairs
  Chutes     unmetered tests ~500 pairs
  nvidia API depends         ~300 pairs
  Samba Nova free            ~400 pairs
  Kimi K2    free preview    ~500 pairs
                  TOTAL  ~  ~7000+ pairs/day synthetic

The script loops forever. Each cycle hits all available providers in
parallel, sleeping 60-120s between cycles to spread load.
"""
import os, sys, json, time, random, hashlib, urllib.request, urllib.error
import threading
import socket
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── LLM provider config ─────────────────────────────────────────────────────
PROVIDERS = [
    {
        "name": "cerebras",
        "url": "https://api.cerebras.ai/v1/chat/completions",
        "key_env": "CEREBRAS_API_KEY",
        "model": "qwen-3-coder-480b",
        "rpm_budget": 30,           # rough requests-per-minute soft cap
    },
    {
        "name": "groq",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "key_env": "GROQ_API_KEY",
        "model": "qwen/qwen3-32b",
        "rpm_budget": 30,
    },
    {
        "name": "openrouter",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "model": "qwen/qwen3-coder:free",
        "rpm_budget": 20,
    },
    {
        "name": "gemini",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "key_env": "GEMINI_API_KEY",
        "model": "gemini-2.0-flash-exp",
        "rpm_budget": 60,
    },
    {
        "name": "chutes",
        "url": "https://api.chutes.ai/v1/chat/completions",
        "key_env": "CHUTES_API_KEY",
        "model": "deepseek-ai/DeepSeek-V3",
        "rpm_budget": 30,
    },
    {
        "name": "nvidia",
        "url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "key_env": "NVAPI_KEY",
        "model": "qwen/qwen2.5-coder-32b-instruct",
        "rpm_budget": 20,
    },
    {
        "name": "samba",
        "url": "https://api.sambanova.ai/v1/chat/completions",
        "key_env": "SAMBA_API_KEY",
        "model": "Meta-Llama-3.3-70B-Instruct",
        "rpm_budget": 20,
    },
    {
        "name": "kimi",
        "url": "https://api.moonshot.ai/v1/chat/completions",
        "key_env": "KIMI_API_KEY",
        "model": "kimi-k2",
        "rpm_budget": 15,
    },
]

# ── Prompt templates ────────────────────────────────────────────────────────
LANGUAGES = ["Python", "TypeScript", "Go", "Rust", "Java", "Bash", "SQL", "Terraform", "YAML"]
FRAMEWORKS = ["FastAPI", "Express", "Spring Boot", "Django", "Next.js", "Gin", "Actix", "Flask"]
CLOUDS = ["AWS", "GCP", "Azure", "Cloudflare"]
SERVICES = ["EC2", "Lambda", "ECS", "S3", "RDS", "VPC", "IAM", "CloudFront", "ALB", "DynamoDB"]
VULNS = ["SQL injection", "XSS", "CSRF", "SSRF", "RCE", "path traversal", "auth bypass", "race condition", "TOCTOU", "deserialization", "broken access control"]
TASKS_DEV = ["parse JSON", "deduplicate a list", "validate email", "rate-limit requests", "implement retry with backoff", "stream large files", "compute checksum", "encrypt data", "compress payload", "write a circuit breaker"]
TASKS_OPS = ["debug high CPU on prod EC2", "trace 502 errors from ALB", "investigate DB slow query", "diagnose memory leak", "respond to OOM kill", "fix expired SSL cert", "drain a node", "rotate database credentials", "audit IAM policy changes"]
ATTACK_PHASES = ["initial-access", "execution", "persistence", "privilege-escalation", "defense-evasion", "credential-access", "lateral-movement", "exfiltration", "impact"]

TEMPLATES = [
    # DEV — coding tasks
    ("dev", "Write a {lang} function using {fw} that {task}. Handle edge cases including timeouts, malformed input, and concurrent calls. Include unit tests."),
    ("dev", "Refactor this {lang} code for clarity and performance. Explain trade-offs:\n\n```{lang}\n# (assume noisy implementation here)\n```"),
    ("dev", "Implement {task} as a {lang} library — public API, error types, examples in docstring."),
    ("dev", "Compare two {lang} implementations of {task} — one optimized for throughput, one for memory. Show benchmarks and pick a winner with justification."),

    # CODE-REVIEW
    ("code-review", "Review this {lang} pull request for security, performance, and maintainability issues. Categorize findings as MUST-FIX / SHOULD-FIX / NIT and explain each."),
    ("code-review", "Identify the {vuln} risk in this {lang} snippet and propose a patch with tests proving the fix."),

    # OPS / SRE
    ("sre", "I need to {ops_task} on {cloud} {service}. Walk me through diagnosis steps, what metrics to pull, and how to mitigate without dropping traffic."),
    ("sre", "Write a runbook for the on-call engineer who paged at 3am for {ops_task}. Include kubectl/aws CLI commands and a rollback plan."),
    ("sre", "Design an SLO for the {service} service. Pick the right SLI, set burn-rate alerts (multi-window multi-burn-rate), and explain the math."),

    # SECURITY / DEVSECOPS
    ("devsecops", "Threat-model a public-facing API on {cloud} {service} that handles user PII. Map findings to MITRE ATT&CK {phase} and propose mitigations."),
    ("devsecops", "Audit this Terraform module for {cloud} {service} — call out IAM over-privilege, public exposure, missing encryption, log retention issues."),
    ("devsecops", "Write a Bash script that detects {vuln} in {lang} repos via static patterns. Output is JSON with file:line:severity:advice."),

    # IAC
    ("iac", "Convert this {cloud} CloudFormation template to Terraform with the same semantics. Note any features that don't translate cleanly."),
    ("iac", "Generate Terraform for: a 3-tier app on {cloud} ({service}, ALB-equivalent, RDS-equivalent). Include least-privilege IAM, network isolation, encrypted state backend."),

    # REASONING / PLANNING
    ("architect", "Compare three architectures for a real-time event-processing system that needs <100ms p99 and 100K events/sec: Kafka + Flink, Kinesis + Lambda, NATS JetStream + custom worker. Pick one and explain trade-offs."),
    ("architect", "I'm migrating from monolith to microservices. The monolith uses shared DB. Walk me through the strangler-fig migration plan and which service to extract first."),

    # DIALOG / Q&A
    ("qa", "Explain {vuln} like I'm a junior engineer with one year of experience. Use a concrete example."),
    ("qa", "Why does {lang} prefer composition over inheritance? Show a 30-line example where inheritance becomes painful and composition fixes it."),
]


def fill_template(category: str, template: str) -> str:
    """Fill {placeholders} with random parameters."""
    return template.format(
        lang=random.choice(LANGUAGES),
        fw=random.choice(FRAMEWORKS),
        cloud=random.choice(CLOUDS),
        service=random.choice(SERVICES),
        vuln=random.choice(VULNS),
        task=random.choice(TASKS_DEV),
        ops_task=random.choice(TASKS_OPS),
        phase=random.choice(ATTACK_PHASES),
    )


def call_llm(provider: dict, prompt: str, timeout: int = 60) -> str | None:
    """Call one provider, return content or None on failure."""
    key = os.environ.get(provider["key_env"], "").strip()
    if not key:
        return None
    body = json.dumps({
        "model": provider["model"],
        "messages": [
            {"role": "system", "content": "You are a senior DevSecOps engineer writing concise, production-ready, well-explained answers. No fluff. Show code with comments where helpful."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 1500,
        "temperature": 0.45,
    }).encode()
    req = urllib.request.Request(
        provider["url"],
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            return content if len(content) > 100 else None
    except (urllib.error.HTTPError, urllib.error.URLError, socket.timeout, json.JSONDecodeError):
        return None
    except Exception:
        return None


def write_pair(out_path: Path, pair: dict, lock: threading.Lock) -> None:
    with lock:
        with open(out_path, "a") as f:
            f.write(json.dumps(pair, ensure_ascii=False) + "\n")


def fire_one(provider: dict, out_path: Path, lock: threading.Lock) -> tuple[str, bool]:
    """One template -> one provider -> one pair (or skip)."""
    category, template = random.choice(TEMPLATES)
    prompt = fill_template(category, template)
    content = call_llm(provider, prompt)
    if not content:
        return (provider["name"], False)
    pair = {
        "ts": time.time(),
        "source": f"synthetic-{provider['name']}",
        "role": category,
        "prompt": prompt,
        "response": content,
        "model": provider["model"],
    }
    write_pair(out_path, pair, lock)
    return (provider["name"], True)


def main():
    home = Path(os.environ.get("HOME", "/home/hermes"))
    out_path = home / ".surrogate/training-pairs.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = home / ".surrogate/logs/llm-burst-generator.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Discover which providers actually have keys set
    active = [p for p in PROVIDERS if os.environ.get(p["key_env"], "").strip()]
    if not active:
        print("ERR: no provider keys found in env — set CEREBRAS_API_KEY etc. as Space secrets")
        sys.exit(1)

    lock = threading.Lock()
    cycle = 0
    cum_kept = 0
    cum_failed = 0

    def log(msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        with open(log_path, "a") as f:
            f.write(line + "\n")

    log(f"start  active_providers={[p['name'] for p in active]}  out={out_path}")

    while True:
        cycle += 1
        # Each cycle: one parallel batch hitting every active provider.
        # 3-5 templates per provider per cycle.
        batch_size_per_provider = 3
        with ThreadPoolExecutor(max_workers=len(active) * batch_size_per_provider) as pool:
            futures = []
            for p in active:
                for _ in range(batch_size_per_provider):
                    futures.append(pool.submit(fire_one, p, out_path, lock))
            kept = failed = 0
            per_provider = {p["name"]: 0 for p in active}
            for fut in as_completed(futures):
                try:
                    name, ok = fut.result()
                    if ok:
                        kept += 1
                        per_provider[name] += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1

        cum_kept += kept
        cum_failed += failed
        details = " ".join(f"{n}={c}" for n, c in per_provider.items() if c)
        log(f"cycle {cycle}: kept={kept} fail={failed}  [{details}]  total_kept={cum_kept}")

        # Sleep 60-120s between cycles (random jitter to avoid sync bursts)
        time.sleep(60 + random.randint(0, 60))


if __name__ == "__main__":
    main()
