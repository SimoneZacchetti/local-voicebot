"""Check that everything the server needs is installed, reachable and fast enough.

Usage: uv run python scripts/check_env.py
"""

from __future__ import annotations

import asyncio
import ipaddress
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.config import POCKET_LANG_NAMES, cfg

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


def is_tailscale(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host.endswith(".ts.net"):
        return True
    try:
        return ipaddress.ip_address(host) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return "." not in host and host != "localhost"  # MagicDNS short name, e.g. "homeassistant"


def check_ha_url() -> None:
    """Home Assistant must be reached over the LAN or a VPN, never exposed on the internet."""
    url = urlsplit(cfg.ha_url)
    host = url.hostname or ""
    if is_tailscale(cfg.ha_url):
        return report(OK, "HA_URL", f"{host} via Tailscale (works at home and away)")
    try:
        public = ipaddress.ip_address(host).is_global
    except ValueError:
        public = not (host.endswith(".local") or "." not in host)
    if public and url.scheme != "https":
        report(WARN, "HA_URL", f"{host} looks public and uses plain http: the token travels unencrypted. Use Tailscale.")
    else:
        report(OK, "HA_URL", f"{host} (local network only)")


async def check_home() -> None:
    if cfg.home_backend != "ha":
        return
    if not cfg.ha_token:
        return report(FAIL, "Home Assistant", "HOME_BACKEND=ha but HA_TOKEN is empty")
    from server.ha import HomeAssistant
    from server.tools import ToolRegistry

    check_ha_url()
    ha = HomeAssistant()
    try:
        tools = await ha.list_tools()
    except httpx.TransportError as e:
        hint = ("is Tailscale connected on this Mac (menu bar icon) and is the Tailscale add-on running in HA?"
                if is_tailscale(cfg.ha_url) else "is Home Assistant on and on the same network?")
        return report(FAIL, f"Home Assistant at {cfg.ha_url}", f"not reachable ({type(e).__name__}): {hint}")
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
    if cfg.home_route != "ha":
        if not google:
            report(FAIL, f"HOME_ROUTE={cfg.home_route}", "needs the Google Assistant SDK integration in Home Assistant")
        elif cfg.home_route == "auto" and not cfg.home_probe:
            report(FAIL, "HOME_ROUTE=auto", "set HOME_PROBE=<ip>:8009 (a Google Home / Chromecast at home)")
        else:
            await reg.update_route()
            how = "forced" if cfg.home_route == "google" else f"HOME_PROBE {cfg.home_probe}"
            report(OK, "home route", f"{reg.mode} ({how}): {', '.join(s['function']['name'] for s in reg.specs)}")


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


def tailscale_cli() -> str | None:
    import shutil

    return shutil.which("tailscale") or next(
        (p for p in ("/Applications/Tailscale.app/Contents/MacOS/Tailscale",) if Path(p).exists()), None)


def check_remote() -> None:
    """Phone access: config sanity, secrets hygiene and what `tailscale serve` publishes."""
    import json
    import subprocess

    from server.__main__ import config_problems

    for p in config_problems():
        report(FAIL, "server config", p)
    if not cfg.remote_enabled:
        return report(OK, "remote access", "disabled (REMOTE_ENABLED=false)")
    root = Path(__file__).resolve().parent.parent
    env = root / ".env"
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", ".env"], check=False, cwd=root, capture_output=True).returncode == 0
    report(FAIL if tracked else OK, ".env in git", "TRACKED: remove it from git now" if tracked else "not tracked")
    if env.exists():
        mode = env.stat().st_mode & 0o777
        report(OK if mode & 0o077 == 0 else WARN, ".env permissions", f"{mode:o}" + ("" if mode & 0o077 == 0 else ": run chmod 600 .env"))
    report(OK if cfg.api_tokens else WARN, "API_TOKENS", f"{len(cfg.api_tokens)} device token(s)" if cfg.api_tokens else "none: Siri Shortcuts disabled")
    cli = tailscale_cli()
    if not cli:
        return report(WARN, "tailscale CLI", "not found: install Tailscale and enable the CLI")
    out = subprocess.run([cli, "serve", "status", "--json"], check=False, capture_output=True, text=True, timeout=10)
    try:
        st = json.loads(out.stdout or "{}")
    except ValueError:
        return report(WARN, "tailscale serve", (out.stderr or out.stdout).strip()[:200])
    proxies = [h.get("Proxy", "") for web in (st.get("Web") or {}).values() for h in (web.get("Handlers") or {}).values()]
    want = f"http://127.0.0.1:{cfg.remote_port}"
    if not proxies:
        report(WARN, "tailscale serve", f"nothing published: run `tailscale serve --bg {cfg.remote_port}`")
    elif all(p.rstrip("/") == want for p in proxies):
        report(OK, "tailscale serve", f"only {want}")
    else:
        report(FAIL, "tailscale serve", f"publishes {proxies}: only {want} is allowed (never PORT {cfg.port})")
    funnel = [k for k, v in (st.get("AllowFunnel") or {}).items() if v]
    report(FAIL if funnel else OK, "tailscale funnel", f"ON for {funnel}: run `tailscale funnel reset`" if funnel else "off")


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
    check_remote()
    print("\nAll good." if not failures else f"\n{failures} problem(s) found.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
