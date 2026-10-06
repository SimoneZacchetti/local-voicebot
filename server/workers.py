"""Single-thread executors per model family, plus a thread -> asyncio streaming bridge.

MLX GPU streams are thread-local (MLX >= 0.31.2), so a model must be loaded, warmed up
and used from one and the same thread. Pocket TTS is not thread-safe either.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

STT_EXEC = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")
TTS_EXEC = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")

_DONE = object()


async def run_in(executor: ThreadPoolExecutor, fn: Callable[..., Any], *args: Any) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, fn, *args)


async def stream_in(
    executor: ThreadPoolExecutor,
    gen_factory: Callable[[threading.Event], Iterator[Any]],
) -> AsyncIterator[Any]:
    """Run a blocking generator in ``executor`` and yield its items asynchronously.

    ``gen_factory`` receives a per-call stop event. When the consumer finishes, breaks
    out or is cancelled (barge-in), the event is set so the generator exits promptly.
    """
    loop = asyncio.get_running_loop()
    stop = threading.Event()
    q: asyncio.Queue[Any] = asyncio.Queue()

    def put(item: Any) -> None:
        loop.call_soon_threadsafe(q.put_nowait, item)

    def produce() -> None:
        try:
            for item in gen_factory(stop):
                if stop.is_set():
                    break
                put(item)
        except BaseException as e:  # noqa: BLE001 - forwarded to the consumer
            put(e)
        finally:
            put(_DONE)

    fut = loop.run_in_executor(executor, produce)
    try:
        while True:
            item = await q.get()
            if item is _DONE:
                break
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        stop.set()
        # Do not await fut on cancellation: the thread finishes on its own shortly.
        if fut.done():
            fut.result()
