"""Check that everything the server needs is installed, reachable and fast enough.

Usage: uv run python scripts/check_env.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.config import POCKET_LANG_NAMES, cfg  # noqa: E402

OK, FAIL, WARN = "\033[32mOK\033[0m", "\033[31mFAIL\033[0m", "\033[33mWARN\033[0m"
failures = 0


def report(status: str, what: str, detail: str = "") -> None:
    global failures
    failures += status == FAIL
    print(f"[{status}] {what}{': ' + detail if detail else ''}")


async def check_ollama() -> None:
    root = cfg.llm_base_url.removesuffix("/").removesuffix("/v1")
    async with httpx.AsyncClient(base_url=root, timeout=120) as c:
        try:
            v = (await c.get("/api/version")).json()["version"]
        except Exception as e:  # noqa: BLE001
            return report(FAIL, "Ollama reachable", f"{root} ({e}). Start the Ollama app.")
        report(OK, "Ollama reachable", f"version {v}")
        tags = [m["name"] for m in (await c.get("/api/tags")).json()["models"]]
        if cfg.llm_model not in tags:
            return report(FAIL, f"model {cfg.llm_model}", f"not pulled. Run: ollama pull {cfg.llm_model}")
        report(OK, f"model {cfg.llm_model}", "pulled")

        from server.llm import OllamaLlm
        from server.prompts import build_messages

        llm = OllamaLlm()
        await llm.warmup()
        msgs = build_messages([{"role": "user", "content": "Ciao! Come ti chiami?"}], "it")
        body = {"model": cfg.llm_model, "messages": msgs, "think": False, "stream": False,
                "options": {"num_predict": 60}}
        r = (await c.post("/api/chat", json=body)).json()
        msg = r.get("message", {})
        if msg.get("thinking"):
            report(FAIL, "thinking disabled", "model still returns 'thinking' content")
        elif not msg.get("content"):
            report(FAIL, "LLM reply", f"empty content: {r}")
        else:
            report(OK, "thinking disabled", repr(msg["content"][:60]))
        t = time.perf_counter()
        async for _ in llm.stream(msgs):
            break
        ttft = (time.perf_counter() - t) * 1000
        report(OK if ttft < 450 else WARN, "LLM time to first token", f"{ttft:.0f} ms")
        ps = (await c.get("/api/ps")).json().get("models", [])
        resident = [m for m in ps if m["name"] == cfg.llm_model]
        if resident:
            report(OK, "model resident in memory", f"expires {resident[0].get('expires_at', '?')}")
        else:
            report(WARN, "model resident in memory", "not loaded; set OLLAMA_KEEP_ALIVE=-1")


def check_stt() -> None:
    from server.stt import ParakeetStt

    stt = ParakeetStt(cfg.stt_model)
    stt._warmup()
    t = time.perf_counter()
    stt._transcribe(np.zeros(16000 * 3, dtype=np.float32))
    report(OK, f"STT {cfg.stt_model}", f"3 s clip in {(time.perf_counter() - t) * 1000:.0f} ms")


def check_tts() -> None:
    import threading

    from server.tts import PocketTts

    tts = PocketTts()
    for lang in cfg.pocket_preload:
        if lang not in POCKET_LANG_NAMES:
            report(WARN, f"TTS {lang}", "no Pocket model: browser speechSynthesis will be used")
            continue
        try:
            tts._ensure(lang)
            list(tts._pcm16_stream(lang, "Ok.", threading.Event()))
            t = time.perf_counter()
            first = next(tts._pcm16_stream(lang, "Questa è una prova della voce.", threading.Event()))
            report(OK, f"TTS {lang} ({cfg.voices[lang]})", f"first chunk {(time.perf_counter() - t) * 1000:.0f} ms, {len(first)} bytes")
        except Exception as e:  # noqa: BLE001
            report(FAIL, f"TTS {lang}", str(e))


async def check_home() -> None:
    if cfg.home_backend != "ha":
        return
    if not cfg.ha_token:
        return report(FAIL, "Home Assistant", "HOME_BACKEND=ha but HA_TOKEN is empty")
    from server.ha import HomeAssistant
    from server.tools import ToolRegistry

    ha = HomeAssistant()
    try:
        tools = await ha.list_tools()
    except Exception as e:  # noqa: BLE001
        return report(FAIL, f"Home Assistant MCP at {cfg.ha_url}{cfg.ha_mcp_path}", f"{e}. Add the 'Model Context Protocol Server' integration.")
    report(OK, "Home Assistant MCP", f"{len(tools)} tools")
    reg = ToolRegistry(ha)
    await reg.start()
    n = reg.context.count("- names:")
    report(OK if n else WARN, "devices exposed to Assist", f"{n}" if n else "none: expose entities in Settings > Voice assistants > Expose")
    google = "google_assistant_sdk.send_text_command" in ha.services
    report(OK if google else WARN, "Google Assistant SDK integration", "available" if google else "not installed (optional)")
    report(OK if reg._has("announce") else WARN, "announcements", "available" if reg._has("announce") else "set HA_ANNOUNCE_PLAYERS or add the Google Assistant SDK integration")


async def check_groq() -> None:
    if not cfg.groq_api_key:
        if "groq" in (cfg.stt_provider, cfg.llm_provider):
            report(FAIL, "Groq", "provider set to groq but GROQ_API_KEY is empty")
        return
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{cfg.groq_base_url}/models", headers={"Authorization": f"Bearer {cfg.groq_api_key}"})
        if r.status_code != 200:
            return report(FAIL, "Groq API key", f"HTTP {r.status_code}")
        ids = {m["id"] for m in r.json()["data"]}
        for model in (cfg.groq_stt_model, cfg.groq_llm_model):
            report(OK if model in ids else FAIL, f"Groq model {model}", "available" if model in ids else "not available")


def main() -> int:
    report(OK if sys.version_info[:2] == (3, 12) else WARN, "Python", sys.version.split()[0])
    asyncio.run(check_ollama())
    check_stt()
    check_tts()
    from server.langid import LangId

    LangId().warmup()
    report(OK, "language detection (lingua)", f"{len(cfg.langid_langs)} languages")
    asyncio.run(check_groq())
    asyncio.run(check_home())
    print("\nAll good." if not failures else f"\n{failures} problem(s) found.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
