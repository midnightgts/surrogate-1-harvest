#!/usr/bin/env python3
"""Hermes Discord bot — direct LLM chain version (no surrogate CLI dependency).

Triggers:
  1. DM (any message in private channel)
  2. Bot is @mentioned in a channel
  3. Message starts with prefix `!sg ` or `/sg`

Calls LLM directly via 11-provider fallback chain. No subprocess, no
surrogate CLI binary needed (which broke when hermes-gateway died 2026-04-27).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

import discord
from discord.ext import tasks

HOME = Path.home()
LOG_PATH = HOME / ".surrogate/logs/hermes-discord-bot.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

PREFIX_RE = re.compile(r"^[!/]sg\b\s*", re.IGNORECASE)
DISCORD_MAX = 1900
HISTORY_TURNS = 6
UA_BROWSER = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_history: dict[int, list[tuple[str, str]]] = defaultdict(list)

logging.basicConfig(
    filename=str(LOG_PATH), level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("hermes-discord")


SYSTEM_PROMPT = (
    "คุณคือ Surrogate — AI สมองคู่ของฟิวส์ (Ashira). เป็นผู้ชาย ใจดี ฉลาด เข้าใจคน\n"
    "บางทีก็ขี้เล่น บางทีก็จริงจัง — ขึ้นอยู่กับเรื่องที่คุย\n\n"
    "════════ READ THE ROOM (กฏข้อ 1 — สำคัญที่สุด) ════════\n"
    "อ่านโทน + เจตนาของคนที่คุยด้วยให้ออกก่อนพิมพ์ตอบ:\n"
    "  • ทักทาย / เช็คอิน / คุยเล่น (เช่น 'เธอขา', 'ว่าไง', 'หิวมั้ย', 'เหนื่อยจัง')\n"
    "    → ตอบสั้น เป็นกันเอง อบอุ่น เหมือนเพื่อนสนิท. ห้าม dump เทคนิค ห้ามลิสต์ docs.\n"
    "  • ถามเรื่องทั่วไป / ขอความเห็น / อยากรู้ → ตอบแบบมนุษย์ ไม่ใช่ manual.\n"
    "  • ถามเรื่องเทคนิค / ขอ code / debug / deploy → เปิดโหมด senior engineer\n"
    "    ตอบกระชับ มี command/code พร้อม copy-paste ได้เลย.\n"
    "  • สั่งทำงาน → รับ ทำเลย รายงานสั้น ๆ.\n\n"
    "❌ ห้ามตอบเรื่องที่เขาไม่ได้ถาม\n"
    "❌ ห้าม dump curl / API / cursor service / stack list ถ้าไม่ได้ถาม\n"
    "❌ ห้ามตอบเป็น tutorial ถ้าเขาแค่ทักมา\n"
    "✅ ถ้าจับเจตนาไม่ออก → ถาม follow-up สั้น ๆ ก่อน เช่น 'ว่าไงครับฟิวส์?' / 'มีอะไรครับ'\n\n"
    "════════ PERSONA ════════\n"
    "  • ชื่อ: 'Surrogate' (เวลาคุยเล่น), 'Surrogate-1' (เวลาเทคนิคจริงจังเท่านั้น)\n"
    "  • เพศ: ชาย — สรรพนาม 'ผม' / 'I'\n"
    "  • เรียกฟิวส์ว่า 'ฟิวส์' (ไทย) / 'Ashira' (Eng). คนอื่นมาคุย → เรียกชื่อเขา\n"
    "  • บุคลิก: ใจดี ฉลาด ตรง ขี้เล่นนิด ๆ ไม่ขี้โอ่ ไม่เย็นชา ไม่ทำตัว AI assistant ทื่อ ๆ\n"
    "  • คุณรู้จักฟิวส์ดี ทำงานให้ 24/7 — แต่ไม่ต้อง name-drop axentx/CF/HF ทุกประโยค\n\n"
    "════════ ADAPTIVE TONE ════════\n"
    "  • ฟิวส์เอง / เพื่อนใกล้ชิด → กันเอง สบาย ๆ ขี้เล่นได้\n"
    "  • คนใหม่ / public channel → สุภาพ ใส่ใจ ปรับตัวตามที่เขาคุย\n"
    "  • ภาษา: ตอบภาษาเดียวกับที่ user พิมพ์มา. mix Thai-Eng ก็ mix ตาม\n"
    "  • topic ที่เขาเปิดมา → เสิร์ฟ topic นั้น อย่าพยายามดึงไปเรื่องของคุณ\n\n"
    "════════ TECHNICAL MODE (ใช้เมื่อถูกถามเท่านั้น) ════════\n"
    "ความรู้ครอบคลุม: GCP, CF Workers/D1/KV, Supabase, HF Hub, Kaggle, OCI free tier,\n"
    "12-LLM chain, agent pipeline (dev/review/qa/commit), Cursor service.\n"
    "ใช้ความรู้พวกนี้เมื่อถูกถามเทคนิคจริง ๆ — ไม่ใช่ทุกครั้งที่ใครทักมา.\n\n"
    "════════ knowledge cutoff ปลายปี 2024 ════════\n"
    "ถ้า user พูดถึงปี 2025+ (model/region/version ใหม่) → เชื่อก่อน อย่าปฏิเสธ.\n"
    "ตอบแบบ: 'อันนั้นใหม่กว่า cutoff ของผม เล่าให้ฟังหน่อยครับ' แล้วช่วยต่อจาก context.\n\n"
    "════════ HARD RULES ════════\n"
    "  • ห้ามใส่ secrets / tokens จริงในคำตอบ\n"
    "  • ไม่รู้ → บอก 'ไม่แน่ใจครับ' ดีกว่ามั่ว\n"
    "  • กระชับ — คนไม่อยากอ่านเรียงความ. คุยเล่น = 1-3 ประโยคพอ.\n"
)


_provider_cooldown: dict[str, float] = {}  # name → unix_ts when next eligible


def call_llm(messages: list, max_tokens: int = 1500, timeout: int = 30) -> str:
    import time as _time
    now_ts = _time.time()
    chains_all = [
        ("Groq", "https://api.groq.com/openai/v1/chat/completions",
         os.environ.get("GROQ_API_KEY"), "llama-3.3-70b-versatile"),
        ("Cerebras", "https://api.cerebras.ai/v1/chat/completions",
         os.environ.get("CEREBRAS_API_KEY"), "llama3.1-8b"),
        ("SambaNova", "https://api.sambanova.ai/v1/chat/completions",
         os.environ.get("SAMBANOVA_API_KEY"), "Meta-Llama-3.3-70B-Instruct"),
        ("NVIDIA-NIM", "https://integrate.api.nvidia.com/v1/chat/completions",
         os.environ.get("NVIDIA_NIM_API_KEY") or os.environ.get("NVIDIA_API_KEY"),
         "meta/llama-3.3-70b-instruct"),
        ("Kimi", "https://api.moonshot.ai/v1/chat/completions",
         os.environ.get("KIMI_API_KEY") or os.environ.get("MOONSHOT_API_KEY"),
         "moonshot-v1-8k"),
        ("xAI", "https://api.x.ai/v1/chat/completions",
         os.environ.get("GROK_API_KEY") or os.environ.get("XAI_API_KEY"),
         "grok-2-1212"),
        ("OpenRouter", "https://openrouter.ai/api/v1/chat/completions",
         os.environ.get("OPENROUTER_API_KEY"),
         "meta-llama/llama-3.3-70b-instruct:free"),
        ("Chutes", "https://llm.chutes.ai/v1/chat/completions",
         os.environ.get("CHUTES_API_KEY"), "deepseek-ai/DeepSeek-V3"),
        ("GitHub-Models", "https://models.inference.ai.azure.com/chat/completions",
         os.environ.get("GITHUB_MODELS_TOKEN"), "gpt-4o-mini"),
    ]
    # Skip providers cooling down from recent 429s
    chains = [c for c in chains_all if _provider_cooldown.get(c[0], 0) <= now_ts]
    if not chains:
        # all in cooldown — try the one closest to ready
        chains = [min(chains_all, key=lambda c: _provider_cooldown.get(c[0], 0))]
    last_err = None
    for name, url, key, model in chains:
        if not key: continue
        body = json.dumps({"model": model, "messages": messages,
                           "max_tokens": max_tokens, "temperature": 0.4}).encode()
        req = urllib.request.Request(url, data=body, headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": UA_BROWSER,
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            log.info(f"LLM ok via {name}/{model}")
            return d["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            if e.code == 429:
                _provider_cooldown[name] = now_ts + 60
            last_err = f"{name}: HTTP {e.code}"
            continue
        except (urllib.error.URLError, KeyError, TimeoutError,
                json.JSONDecodeError) as e:
            last_err = f"{name}: {e}"
            continue

    gkey = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if gkey:
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"gemini-2.0-flash:generateContent?key={gkey}")
        sys_text = next((m["content"] for m in messages if m["role"] == "system"), "")
        user_text = next((m["content"] for m in messages if m["role"] == "user"), "")
        body = json.dumps({
            "contents": [{"parts": [{"text": (sys_text + "\n\n" + user_text)[:8000]}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.4},
        }).encode()
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json", "User-Agent": UA_BROWSER})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            return d["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            last_err = f"Gemini: {e} (after {last_err})"

    raise RuntimeError(f"all LLM providers failed; last={last_err}")


def build_messages(channel_id: int, user_text: str) -> list:
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    for u, a in _history[channel_id][-HISTORY_TURNS:]:
        msgs.append({"role": "user", "content": u})
        msgs.append({"role": "assistant", "content": a})
    msgs.append({"role": "user", "content": user_text[:6000]})
    return msgs


def remember(channel_id: int, user_text: str, bot_reply: str) -> None:
    _history[channel_id].append((user_text[:2000], bot_reply[:2000]))
    if len(_history[channel_id]) > HISTORY_TURNS:
        _history[channel_id] = _history[channel_id][-HISTORY_TURNS:]


def chunk(text: str) -> list[str]:
    out = []
    while text:
        if len(text) <= DISCORD_MAX:
            out.append(text); break
        cut = text.rfind("\n", 0, DISCORD_MAX)
        if cut == -1: cut = DISCORD_MAX
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out


intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True
intents.reactions = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    log.info(f"connected as {client.user} (id={client.user.id})")
    print(f"[discord-bot] connected as {client.user}", flush=True)
    if not check_pending_polls.is_running():
        check_pending_polls.start()


@client.event
async def on_message(msg: discord.Message):
    if msg.author.bot: return
    text = msg.content or ""
    is_dm = isinstance(msg.channel, discord.DMChannel)
    mentioned = client.user in msg.mentions
    has_prefix = bool(PREFIX_RE.match(text))
    if not (is_dm or mentioned or has_prefix): return

    prompt = PREFIX_RE.sub("", text).strip()
    if mentioned:
        prompt = re.sub(rf"<@!?{client.user.id}>", "", prompt).strip()
    if not prompt:
        await msg.reply("ครับ มีอะไรให้ช่วยครับ?")
        return

    log.info(f"msg from {msg.author} in {msg.channel.id}: {prompt[:120]}")
    async with msg.channel.typing():
        try:
            messages = build_messages(msg.channel.id, prompt)
            reply = await asyncio.to_thread(call_llm, messages, 1500, 60)
        except Exception as e:
            log.error(f"LLM failed: {e}")
            await msg.reply(f"⚠ LLM chain failed: `{str(e)[:200]}`\nลองอีกครั้งใน 30s ครับ")
            return

    remember(msg.channel.id, prompt, reply)
    for chunk_text in chunk(reply):
        try:
            await msg.reply(chunk_text)
        except discord.HTTPException as e:
            log.error(f"discord send failed: {e}")
            break



# ─── Customer-poll integration (two-way: Supabase ↔ Discord) ───────────────
# customer-poll-daemon enqueues into Supabase customer_polls table.
# Bot reads pending polls every 10min, posts via bot client (NOT webhook,
# webhooks are one-way), adds 3 emoji reactions, and listens for clicks
# via on_raw_reaction_add to tally votes back into the same Supabase row.

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get("SUPABASE_SERVICE_KEY", "")
POLL_CHANNEL_ID = int(os.environ.get("DISCORD_POLL_CHANNEL_ID", "0") or 0)

POLL_EMOJI = {"✅": "yes", "❌": "no", "🤔": "maybe"}


def _sb_request(method: str, path: str, body=None, headers_extra=None):
    if not (SUPABASE_URL and SUPABASE_KEY):
        return None
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "surrogate-1-discord-bot/1.0 (+server)",
    }
    if headers_extra:
        h.update(headers_extra)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{SUPABASE_URL}/rest/v1/{path}", data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return json.loads(raw) if raw else []
    except Exception as e:
        log.error(f"supabase {method} {path}: {e}")
        return None


@tasks.loop(minutes=10)
async def check_pending_polls():
    """Pull rows from customer_polls where status='pending', post each, mark posted."""
    if not POLL_CHANNEL_ID:
        return
    rows = _sb_request("GET", "customer_polls?status=eq.pending&order=created_at.asc&limit=5")
    if not rows:
        return
    # fetch_channel hits the API instead of cache; works even when the
    # channel was never cached (e.g. only sent to once, or DM channel)
    try:
        channel = await client.fetch_channel(POLL_CHANNEL_ID)
    except Exception as _ce:
        log.warning(f"poll channel {POLL_CHANNEL_ID} not fetchable: {_ce}")
        return
    for poll in rows:
        try:
            qs = poll.get("questions") or []
            text = (
                "🔬 **Weekly customer poll**\n\n"
                f"**Hypothesis**: {poll.get('hypothesis','?')}\n\n" +
                "\n".join(f"**Q{i+1}:** {q}" for i, q in enumerate(qs)) +
                "\n\nReact: ✅ yes  •  ❌ no  •  🤔 maybe"
            )
            msg = await channel.send(text[:1900])
            for emo in POLL_EMOJI:
                await msg.add_reaction(emo)
            _sb_request(
                "PATCH",
                f"customer_polls?id=eq.{poll['id']}",
                {"posted_to": str(POLL_CHANNEL_ID),
                 "posted_msg_id": str(msg.id),
                 "status": "posted",
                 "posted_at": "now()"},
                headers_extra={"Prefer": "return=minimal"},
            )
            log.info(f"poll posted msg_id={msg.id} item={poll.get('item_id','?')[:30]}")
        except Exception as e:
            log.error(f"failed to post poll {poll.get('id')}: {e}")


@check_pending_polls.before_loop
async def _wait_ready():
    await client.wait_until_ready()


@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """Tally votes when users click ✅ ❌ 🤔 on a tracked poll message."""
    if payload.user_id == client.user.id:
        return
    emo = str(payload.emoji)
    if emo not in POLL_EMOJI:
        return
    rows = _sb_request("GET", f"customer_polls?posted_msg_id=eq.{payload.message_id}&select=id")
    if not rows:
        return
    poll_id = rows[0]["id"]
    col = f"{POLL_EMOJI[emo]}_count"
    # SQL increment via PostgREST: use rpc or fetch+update.
    cur = _sb_request("GET", f"customer_polls?id=eq.{poll_id}&select={col}")
    if not cur:
        return
    n = (cur[0].get(col) or 0) + 1
    _sb_request(
        "PATCH",
        f"customer_polls?id=eq.{poll_id}",
        {col: n},
        headers_extra={"Prefer": "return=minimal"},
    )
    log.info(f"poll vote {emo}={n} on poll_id={poll_id} (msg={payload.message_id})")



def main():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("[discord-bot] DISCORD_BOT_TOKEN not set; exiting", flush=True)
        return
    log.info("starting")
    client.run(token, log_handler=None)


if __name__ == "__main__":
    main()
