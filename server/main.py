"""FastAPI app: serves the web client and the /ws voice conversation endpoint."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles

from .config import cfg
from .langid import LangId
from .llm import LlmRouter
from .prompts import set_home_context
from .session import Services, Session
from .stt import SttRouter
from .tools import ToolRegistry
from .tts import PocketTts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for noisy in ("httpx", "pocket_tts", "root", "huggingface_hub"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("voicebot")
log.setLevel(logging.INFO)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def build_services() -> Services:
    ha = None
    if cfg.home_backend == "ha":
        from .ha import HomeAssistant

        ha = HomeAssistant()
    return Services(stt=SttRouter(), llm=LlmRouter(), tts=PocketTts(), langid=LangId(), tools=ToolRegistry(ha))


async def start_tools(svc: Services) -> bool:
    ok = await svc.tools.start()
    await svc.tools.update_route()
    svc.llm.set_tools(svc.tools.specs)
    set_home_context(svc.tools.context)
    return ok


async def retry_tools(svc: Services) -> None:
    """Home Assistant was unreachable at startup (VPN down, HA rebooting): keep trying in the background."""
    while True:
        await asyncio.sleep(cfg.ha_retry_s)
        if await start_tools(svc):
            log.info("Home Assistant reachable again: home_tools=%d", len(svc.tools.specs))
            return


async def warmup(svc: Services) -> bool:
    t = time.perf_counter()
    # Tools first: the LLM warm-up must use the exact same tool list as real turns.
    tools_ok = await start_tools(svc)

    async def timed(name: str, coro) -> None:
        t0 = time.perf_counter()
        try:
            await coro
            log.info("warm-up %s ok (%.1fs)", name, time.perf_counter() - t0)
        except Exception as e:  # noqa: BLE001
            log.error("warm-up %s FAILED: %s", name, e)

    await asyncio.gather(
        timed("stt", svc.stt.warmup()),
        timed("tts", svc.tts.preload(cfg.pocket_preload)),
        timed("llm", svc.llm.warmup()),
        timed("langid", asyncio.to_thread(svc.langid.warmup)),
    )
    log.info("ready in %.1fs: stt=%s llm=%s (%s) home_tools=%d", time.perf_counter() - t, svc.stt.name, svc.llm.name,
             cfg.groq_llm_model if svc.llm.name == "groq" else cfg.llm_model, len(svc.tools.specs))
    return tools_ok


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.svc = build_services()
    tools_ok = await (warmup if cfg.warmup else start_tools)(app.state.svc)
    retry = None if tools_ok or cfg.ha_retry_s <= 0 else asyncio.create_task(retry_tools(app.state.svc))
    yield
    if retry:
        retry.cancel()


app = FastAPI(lifespan=lifespan)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    await Session(ws, app.state.svc).run()


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


# Mounted last so it does not shadow /ws and /healthz.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
