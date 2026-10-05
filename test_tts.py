"""
Self-check for the /tts fallback chain: Gemini TTS first, Edge TTS on failure.

Run: python test_tts.py
"""
import asyncio
import base64
import io
import os
import wave

from dotenv import load_dotenv

load_dotenv()

from ai_interview import _tts_edge, _tts_gemini, generate_tts, TTSPayload


async def main():
    # 1. Edge TTS must always work -- it is the safety net.
    edge = await _tts_edge("Halo, nama saya Budi.")
    assert len(base64.b64decode(edge)) > 1000, "Edge TTS returned no audio"
    print(f"[ok] Edge TTS       -> {len(base64.b64decode(edge))} bytes mp3")

    # 2. Gemini TTS: may legitimately fail (region block / no key). Both the
    #    success and the failure path are acceptable, but a success must be
    #    playable WAV.
    try:
        b64 = await _tts_gemini("Halo, nama saya Budi.")
        raw = base64.b64decode(b64)
        with wave.open(io.BytesIO(raw)) as wf:
            assert wf.getframerate() == 24000 and wf.getnchannels() == 1
        print(f"[ok] Gemini TTS     -> {len(raw)} bytes wav")
    except Exception as e:
        print(f"[--] Gemini TTS     -> unavailable ({type(e).__name__}), fallback will be used")

    # 3. The endpoint must always return playable audio, whichever path ran,
    #    and the format must match so the browser builds the right data URI.
    res = await generate_tts(TTSPayload(text="Halo, nama saya Budi."))
    assert "error" not in res, f"endpoint failed: {res}"
    assert res["format"] in ("wav", "mp3"), res["format"]
    assert len(base64.b64decode(res["audio_base64"])) > 1000
    print(f"[ok] POST /tts      -> format={res['format']}")

    # 4. Force a Gemini failure and confirm the endpoint still serves audio.
    saved, os.environ["GEMINI_API_KEY"] = os.getenv("GEMINI_API_KEY", ""), ""
    try:
        res = await generate_tts(TTSPayload(text="Uji cadangan."))
        assert res.get("format") == "mp3", f"expected Edge fallback, got {res}"
        print("[ok] fallback        -> Edge TTS served audio when Gemini failed")
    finally:
        os.environ["GEMINI_API_KEY"] = saved

    # 5. Empty input stays rejected.
    assert "error" in await generate_tts(TTSPayload(text="   "))
    print("[ok] empty text      -> rejected")

    print("\nAll checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
