"""Measure LLM time-to-first-token and tokens/s through the same client the server uses.

Usage: uv run python scripts/bench_llm.py [model ...]
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.config import cfg  # noqa: E402
from server.llm import OllamaLlm  # noqa: E402
from server.prompts import build_messages  # noqa: E402

QUESTIONS = [
    ("it", "Ciao! Mi consigli un posto carino da visitare in Toscana?"),
    ("en", "What's a quick tip to sleep better?"),
    ("it", "Quanto fa diciassette per ventitré?"),
    ("fr", "Comment dit-on merci en japonais ?"),
]


async def bench(model: str) -> None:
    cfg.llm_model = model
    llm = OllamaLlm()
    t = time.perf_counter()
    await llm.warmup()
    print(f"\n== {model} (warm-up/load {time.perf_counter() - t:.1f}s)")
    ttfts = []
    for lang, q in QUESTIONS * 2:
        msgs = build_messages([{"role": "user", "content": q}], lang)
        t0 = time.perf_counter()
        first = None
        text = ""
        n = 0
        async for d in llm.stream(msgs):
            if first is None:
                first = time.perf_counter() - t0
            text += d
            n += 1
        total = time.perf_counter() - t0
        if first is None:
            print(f"  [{lang}] EMPTY REPLY (thinking not disabled?)")
            continue
        ttfts.append(first * 1000)
        rate = n / (total - first) if total > first else 0
        print(f"  [{lang}] ttft={first * 1000:4.0f}ms total={total:4.2f}s ~{rate:4.0f} chunk/s | {text.strip()[:110]!r}")
    if ttfts:
        print(f"  TTFT p50={statistics.median(ttfts):.0f}ms max={max(ttfts):.0f}ms")


async def main() -> None:
    for model in sys.argv[1:] or [cfg.llm_model]:
        await bench(model)


if __name__ == "__main__":
    asyncio.run(main())
