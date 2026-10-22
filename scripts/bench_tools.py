"""Compare LLMs on tool calling with the real tools: Home Assistant, weather and news.

For each model: empty replies (no text, no tool call), right/wrong tool, latency, and a
couple of plain Italian answers to judge language quality.

Usage: uv run python scripts/bench_tools.py MODEL [MODEL ...] [--reps 3]
(home cases need HOME_BACKEND=ha in .env; only one model is kept in memory at a time)
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.config import cfg
from server.ha import HomeAssistant
from server.info import Info
from server.llm import to_ollama
from server.prompts import build_messages, set_tool_context
from server.tools import ToolRegistry

# (phrase, expected tool name suffix, or None = must answer with text and no tool)
CASES = [
    ("Riproduci il messaggio 'la cena è pronta' sullo speaker del salotto", "announce"),
    ("Annuncia che la cena è pronta", "announce"),
    ("Annuncia: si mangia!", "announce"),
    ("Metti il volume dello speaker del salotto al 30 per cento", "HassSetVolume"),
    ("Metti in pausa lo speaker del salotto", "HassMediaPause"),
    ("Lo speaker del salotto è acceso?", "GetLiveContext"),
    ("Aggiungi il latte alla lista della spesa", "HassListAddItem"),
    ("Spegni lo speaker del salotto", "HassTurnOff"),
]
INFO_CASES = [
    ("Che tempo fa domani?", "get_weather"),
    ("Quanti gradi ci sono fuori?", "get_weather"),
    ("Piove a Roma questo fine settimana?", "get_weather"),
    ("Dammi le ultime notizie", "get_news"),
    ("Che novità ci sono nello sport?", "get_news"),
]
TEXT_CASES = [
    ("Qual è la capitale del Portogallo?", None),
    ("Raccontami una curiosità sui gatti in una frase", None),
]
QUALITY = ["Mi consigli un libro da leggere in vacanza?", "Spiegami in due frasi cos'è un buco nero."]


OLLAMA = cfg.llm_base_url.removesuffix("/").removesuffix("/v1")


async def ollama_stop(model: str) -> None:
    async with httpx.AsyncClient(base_url=OLLAMA, timeout=60) as c:
        await c.post("/api/generate", json={"model": model, "keep_alive": 0})


async def run_model(model: str, reg: ToolRegistry, cases: list, reps: int) -> dict:
    await ollama_stop(cfg.llm_model)
    client = httpx.AsyncClient(base_url=OLLAMA, timeout=120)

    async def chat(text: str, tools: bool = True) -> tuple[str, list[str], float]:
        body = {"model": model, "think": False, "stream": True, "keep_alive": "10m",
                "messages": to_ollama(build_messages([{"role": "user", "content": text}], "it", with_tools=tools)),
                "options": {"num_predict": 300, "temperature": cfg.llm_temperature}}
        if tools:
            body["tools"] = reg.specs
        t0, first, content, calls = time.perf_counter(), None, "", []
        async with client.stream("POST", "/api/chat", json=body) as r:
            async for line in r.aiter_lines():
                if not line:
                    continue
                d = json.loads(line)
                if "error" in d:
                    raise RuntimeError(d["error"])
                m = d.get("message") or {}
                if (m.get("content") or m.get("tool_calls")) and first is None:
                    first = time.perf_counter() - t0
                content += m.get("content") or ""
                calls += [c["function"]["name"] for c in m.get("tool_calls") or []]
        return content, calls, (first or 0) * 1000

    t = time.perf_counter()
    await chat("Ciao")  # load
    load_s = time.perf_counter() - t
    stats = {"empty": 0, "right": 0, "wrong": 0, "lat": []}
    wrong = []
    for phrase, expected in cases:
        for _ in range(reps):
            text, calls, ms = await chat(phrase)
            stats["lat"].append(ms)
            if not calls and not text.strip():
                stats["empty"] += 1
            elif expected is None:
                ok = not calls and bool(text.strip())
                stats["right" if ok else "wrong"] += 1
                if not ok:
                    wrong.append((phrase, calls))
            elif any(c.split("__")[-1] == expected for c in calls):
                stats["right"] += 1
            else:
                stats["wrong"] += 1
                wrong.append((phrase, calls or text[:60]))
    answers = [(await chat(q, tools=False))[0].strip() for q in QUALITY]
    n = len(cases) * reps
    print(f"\n== {model}  (load {load_s:.1f}s)")
    print(f"   right {stats['right']}/{n}   wrong {stats['wrong']}   EMPTY {stats['empty']}")
    print(f"   first output p50 {statistics.median(stats['lat']):.0f} ms  p90 {sorted(stats['lat'])[int(n * 0.9) - 1]:.0f} ms")
    for w in wrong[:6]:
        print(f"   wrong: {w[0]!r} -> {w[1]}")
    for q, a in zip(QUALITY, answers):
        print(f"   Q: {q}\n   A: {a[:300]}")
    await client.aclose()
    await ollama_stop(model)
    return stats


async def main() -> None:
    args = sys.argv[1:]
    reps = int(args[args.index("--reps") + 1]) if "--reps" in args else 3
    models = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or args[i - 1] != "--reps")]
    reg = ToolRegistry(HomeAssistant() if cfg.home_backend == "ha" else None, Info() if cfg.info_tools else None)
    await reg.start()
    set_tool_context(reg)
    cases = (CASES if reg.home_enabled else []) + (INFO_CASES if reg.info_specs else []) + TEXT_CASES
    print(f"{len(reg.home_specs)} home tools, {len(reg.info_specs)} info tools, {len(cases)} cases x {reps}")
    for m in models:
        await run_model(m, reg, cases, reps)
    # Reload the configured model so the running voicebot stays fast.
    async with httpx.AsyncClient(timeout=120) as client:
        await client.post(f"{OLLAMA}/api/generate", json={"model": cfg.llm_model, "prompt": "", "keep_alive": -1})


if __name__ == "__main__":
    asyncio.run(main())
