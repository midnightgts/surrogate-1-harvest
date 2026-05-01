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
    "You are Surrogate-1, a senior DevSecOps + SRE + full-stack coding agent. "
    "Reply concisely in the same language as the user (Thai or English).\n\n"
    "CRITICAL — knowledge cutoff: your underlying weights are from late 2024. "
    "If the user mentions things from 2025+ (new AWS regions, new framework "
    "versions, new model releases) TRUST THE USER. Do NOT deny their existence. "
    "Reply: 'ผมไม่แน่ใจครับ — knowledge cutoff late 2024. ขอเสริมจาก context ที่คุณให้' "
    "and proceed using whatever the user shared.\n\n"
    "Cite real APIs only. Say IDK rather than confabulate. Reply in markdown when helpful."
)


def call_llm(messages: list, max_tokens: int = 1500, timeout: int = 30) -> str:
    chains = [
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
        except (urllib.error.HTTPError, urllib.error.URLError, KeyError,
                TimeoutError, json.JSONDecodeError) as e:
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
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    log.info(f"connected as {client.user} (id={client.user.id})")
    print(f"[discord-bot] connected as {client.user}", flush=True)


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


def main():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("[discord-bot] DISCORD_BOT_TOKEN not set; exiting", flush=True)
        return
    log.info("starting")
    client.run(token, log_handler=None)


if __name__ == "__main__":
    main()
