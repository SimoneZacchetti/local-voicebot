"""Check that everything the server needs is installed, reachable and fast enough.

Usage: uv run python scripts/check_env.py
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.config import IS_APPLE_SILICON, POCKET_LANG_NAMES, cfg

WINDOWS = sys.platform == "win32"


def use_colors() -> bool:
    if not sys.stdout.isatty() or "NO_COLOR" in os.environ:
        return False
    if not WINDOWS:
        return True
    try:  # Turn on ANSI escape processing in the Windows console (ENABLE_VIRTUAL_TERMINAL_PROCESSING).
        import ctypes

        k32 = ctypes.windll.kernel32
        handle, mode = k32.GetStdHandle(-11), ctypes.c_uint32()
        return bool(k32.GetConsoleMode(handle, ctypes.byref(mode)) and k32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:  # noqa: BLE001
        return False


sys.stdout.reconfigure(errors="replace")
if use_colors():
    OK, FAIL, WARN = "\033[32mOK\033[0m", "\033[31mFAIL\033[0m", "\033[33mWARN\033[0m"
else:
    OK, FAIL, WARN = "OK", "FAIL", "WARN"
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
        tag = cfg.llm_model.lower()
        if not IS_APPLE_SILICON and any(s in tag for s in ("nvfp4", "mxfp8", "-mlx")):
            return report(FAIL, f"model {cfg.llm_model}", "this tag runs only on Apple Silicon. Use a GGUF tag: "
                          "LLM_MODEL=qwen3.6:35b-a3b (or qwen3.5:9b with 16 GB RAM, qwen3.5:4b on low-end machines)")
        tags = [m["name"] for m in (await c.get("/api/tags")).json()["models"]]
        if cfg.llm_model not in tags:
            return report(FAIL, f"model {cfg.llm_model}", f"not pulled. Run: ollama pull {cfg.llm_model}")
        report(OK, f"model {cfg.llm_model}", "pulled")

        caps = set((await c.post("/api/show", json={"model": cfg.llm_model})).json().get("capabilities") or [])
        thinking = "thinking" in caps
        if caps:
            if "tools" not in caps and cfg.home_backend == "ha":
                report(FAIL, "LLM tool calling", "model does not support tools, needed by HOME_BACKEND=ha")
            if not thinking and cfg.llm_think != "off":
                report(WARN, "LLM thinking", "model does not support thinking: set LLM_THINK=off in .env")
            report(OK, "LLM capabilities", ", ".join(sorted(caps)))

        from server.llm import OllamaLlm
        from server.prompts import build_messages

        msgs = build_messages([{"role": "user", "content": "Ciao! Come ti chiami?"}], "it")
        body = {"model": cfg.llm_model, "messages": msgs, "stream": False, "options": {"num_predict": 60}}
        if thinking or not caps:
            body["think"] = False
        r = (await c.post("/api/chat", json=body)).json()
        msg = r.get("message", {})
        if msg.get("thinking"):
            report(FAIL, "thinking disabled", "model still returns 'thinking' content")
        elif not msg.get("content"):
            report(FAIL, "LLM reply", f"empty content: {r}")
        else:
            report(OK, "thinking disabled", repr(msg["content"][:60]))
        try:
            llm = OllamaLlm()
            await llm.warmup()
            t = time.perf_counter()
            async for _ in llm.stream(msgs):
                break
            ttft = (time.perf_counter() - t) * 1000
            report(OK if ttft < 450 else WARN, "LLM time to first token", f"{ttft:.0f} ms")
        except Exception as e:  # noqa: BLE001
            report(FAIL, "LLM streaming (server client)", str(e)[:200])
        ps = (await c.get("/api/ps")).json().get("models", [])
        resident = [m for m in ps if m["name"] == cfg.llm_model]
        if resident:
            report(OK, "model resident in memory", f"expires {resident[0].get('expires_at', '?')}")
        else:
            hint = ("[Environment]::SetEnvironmentVariable('OLLAMA_KEEP_ALIVE', '-1', 'User') in PowerShell, "
                    "then quit and reopen Ollama from the tray" if WINDOWS
                    else "launchctl setenv OLLAMA_KEEP_ALIVE -1, then restart Ollama" if sys.platform == "darwin"
                    else "set OLLAMA_KEEP_ALIVE=-1 in the Ollama service environment")
            report(WARN, "model resident in memory", f"not loaded; {hint}")


VC_HINT = "install the latest Microsoft Visual C++ runtime: https://aka.ms/vs/17/release/vc_redist.x64.exe"


def is_dll_error(e: Exception) -> bool:
    # A plain OSError (not FileNotFoundError etc.) is what ctypes raises for a DLL that fails to load.
    return WINDOWS and ("DLL load failed" in str(e) or type(e) is OSError or not str(e))


def check_stt() -> None:
    what = f"STT {cfg.stt_backend} {cfg.stt_model}"
    try:
        from server.stt import make_local_stt

        stt = make_local_stt()
        stt._warmup()
        t = time.perf_counter()
        stt._transcribe(np.zeros(16000 * 3, dtype=np.float32))
    except Exception as e:  # noqa: BLE001
        hint = VC_HINT if is_dll_error(e) else "run `uv sync` and check STT_BACKEND / STT_MODEL in .env"
        return report(FAIL, what, f"{type(e).__name__}: {str(e)[:200]}. Hint: {hint}")
    report(OK, what, f"3 s clip in {(time.perf_counter() - t) * 1000:.0f} ms")


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
            report(FAIL, f"TTS {lang}", f"{e}. Hint: {VC_HINT}" if is_dll_error(e) else str(e))


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
        hint = ("is Tailscale connected on this computer and is the Tailscale add-on running in HA?"
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


async def check_info() -> None:
    if not cfg.info_tools:
        return report(OK, "weather and news", "off (INFO_TOOLS empty): fully offline")
    from server.info import Info

    info = Info()
    try:
        if "get_weather" in info.names:
            try:
                place = await info.here(cfg.default_lang, None)
            except httpx.HTTPError as e:
                return report(FAIL, "weather", f"unreachable: {type(e).__name__}")
            if place is None:
                report(WARN, "weather", "no position: set WEATHER_LOCATION (the browser position still works)")
            else:
                res = await info.execute("get_weather", {}, cfg.default_lang)
                where = {"config": "WEATHER_LOCATION", "ip": "public IP, approximate"}.get(place.source, place.source)
                report(OK if res["ok"] else FAIL, "weather (Open-Meteo)",
                       f"{res.get('place')} ({where}): {res.get('now', {}).get('temp_c')} °C" if res["ok"] else res["error"])
        if "get_news" in info.names:
            res = await info.execute("get_news", {}, cfg.default_lang)
            report(OK if res["ok"] else WARN, f"news ({cfg.default_lang})",
                   f"{res['source']}, {len(res['headlines'])} headlines" if res["ok"] else res["error"])
    finally:
        await info.client.aclose()


def tailscale_cli() -> str | None:
    candidates = (Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale"),
                  Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale" / "tailscale.exe")
    return shutil.which("tailscale") or next((str(p) for p in candidates if p.exists()), None)


# Everyone, Authenticated Users, BUILTIN\Users: SIDs, because account names are localized.
BROAD_SIDS = {"S-1-1-0": "Everyone", "S-1-5-11": "Authenticated Users", "S-1-5-32-545": "Users"}


def check_env_permissions(env: Path) -> None:
    if not WINDOWS:
        mode = env.stat().st_mode & 0o777
        return report(OK if mode & 0o077 == 0 else WARN, ".env permissions",
                      f"{mode:o}" + ("" if mode & 0o077 == 0 else ": run chmod 600 .env"))
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if not ps:
        return
    # First line: the current user's SID, so the hint works in cmd and PowerShell (and for domain/Entra accounts).
    cmd = ("[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
           "(Get-Acl -LiteralPath $env:VOICEBOT_ENV).Access | ForEach-Object "
           "{ $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value }")
    out = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", cmd], check=False, capture_output=True,
                         text=True, timeout=30, env={**os.environ, "VOICEBOT_ENV": str(env)})
    if out.returncode != 0:
        return
    me, *sids = out.stdout.split() or [""]
    broad = sorted({BROAD_SIDS[s] for s in sids if s in BROAD_SIDS})
    if broad:
        report(WARN, ".env permissions", f"readable by {', '.join(broad)}: run "
               f'icacls .env /inheritance:r /grant:r "*{me}:(M)"')
    else:
        report(OK, ".env permissions", "only your account and administrators")


def check_remote() -> None:
    """Phone access: config sanity, secrets hygiene and what `tailscale serve` publishes."""
    import json

    from server.__main__ import config_problems

    for p in config_problems():
        report(FAIL, "server config", p)
    if not cfg.remote_enabled:
        return report(OK, "remote access", "disabled (REMOTE_ENABLED=false)")
    root = Path(__file__).resolve().parent.parent
    env = root / ".env"
    git = shutil.which("git")
    if not git:
        report(WARN, ".env in git", "git not found, skipped")
    else:
        tracked = subprocess.run([git, "ls-files", "--error-unmatch", ".env"], check=False, cwd=root, capture_output=True).returncode == 0
        report(FAIL if tracked else OK, ".env in git", "TRACKED: remove it from git now" if tracked else "not tracked")
    if env.exists():
        check_env_permissions(env)
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
    asyncio.run(check_info())
    asyncio.run(check_home())
    check_remote()
    print("\nAll good." if not failures else f"\n{failures} problem(s) found.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
