"""Self-check: /tts falls back without retrying a broken Gemini, and the
interview template prefetches every line it speaks.

Run: python test_tts_prefetch.py
"""
import asyncio
import re

import ai_interview as ai


# --- 1. Gemini is tried once, then skipped forever (the latency win) ---------
calls = {"gemini": 0, "edge": 0}


async def _boom(text):
    calls["gemini"] += 1
    raise RuntimeError("region blocked")


async def _edge(text):
    calls["edge"] += 1
    return "ZmFrZQ=="


ai._tts_gemini = _boom
ai._tts_edge = _edge

payload = ai.TTSPayload(text="halo")
for _ in range(3):
    result = asyncio.run(ai.generate_tts(payload))

assert calls["gemini"] == 1, f"Gemini retried {calls['gemini']}x; should short-circuit after 1"
assert calls["edge"] == 3, calls
assert result["format"] == "mp3", result

# Empty text never hits a provider.
assert "error" in asyncio.run(ai.generate_tts(ai.TTSPayload(text="   ")))
assert calls["edge"] == 3, "empty text should not reach TTS"


# --- 2. Every spoken constant is also prefetched ----------------------------
html = open("templates/interview.html", encoding="utf-8").read()

spoken = set(re.findall(r"speak\((\w+)[,)]", html))
prefetched = set(re.findall(r"prefetchTTS\((\w+)[,)]", html))
spoken -= {"utterance"}  # browser-synthesis fallback, no network fetch
assert spoken <= prefetched | {"text"}, f"spoken but never prefetched: {spoken - prefetched}"

# speak() must read through the cache, not fetch directly.
assert "await prefetchTTS(text)" in html, "speak() bypasses the TTS cache"

# The removed live-transcript panel must leave no dangling references.
assert "transcript-preview" not in html, "stale transcript element reference"
assert "setTranscriptPreview" not in html, "stale transcript helper reference"

print("OK")
