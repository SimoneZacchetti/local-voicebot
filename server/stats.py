"""Latency log: one SQLite row per finished turn (data/), summarised as p50/p95 for /stats.

Writes go through a single-thread executor so the event loop never waits on disk, and a
failed write only costs a log line: stats must never break a conversation.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger("voicebot.stats")

# Turn metrics shown on the page, in pipeline order (keys of Turn.metrics, server/session.py).
METRICS = ("stt_ms", "llm_ttft_ms", "llm_tool_call_ms", "llm_after_tools_ttft_ms", "first_audio_ms", "total_ms")
SOURCES = ("voice", "chat")
RECENT = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    ts REAL NOT NULL,          -- unix time the turn finished
    source TEXT NOT NULL,      -- voice | chat
    lang TEXT NOT NULL,
    llm TEXT NOT NULL,         -- provider that answered (ollama, groq...)
    actions INTEGER NOT NULL,  -- home actions executed
    metrics TEXT NOT NULL      -- JSON: Turn.metrics
);
CREATE INDEX IF NOT EXISTS turns_ts ON turns (ts);
"""


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile (``p`` in 0..100) of ``values``, None when empty."""
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, -(-len(s) * p // 100) - 1))  # ceil(n*p/100) - 1
    return s[int(k)]


def describe(values: list[float]) -> dict:
    return {"n": len(values), "p50": percentile(values, 50), "p95": percentile(values, 95),
            "max": max(values) if values else None}


class Stats:
    def __init__(self, path: str | Path, retention_days: float = 90) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.lock = threading.Lock()  # writer thread vs summary() on the request thread
        self.exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stats")
        with self.lock, self.db:
            self.db.executescript(SCHEMA)
            if retention_days > 0:
                self.db.execute("DELETE FROM turns WHERE ts < ?", (time.time() - retention_days * 86400,))

    def record(self, source: str, lang: str, llm: str, actions: int, metrics: dict) -> None:
        """Queue one finished turn; returns at once."""
        row = (time.time(), source, lang, llm, actions, json.dumps(metrics))
        try:
            self.exec.submit(self._insert, row)
        except RuntimeError:  # closed: a turn finishing during shutdown
            log.warning("stats closed, turn not recorded")

    def _insert(self, row: tuple) -> None:
        try:
            with self.lock, self.db:
                self.db.execute("INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?)", row)
        except Exception as e:  # noqa: BLE001
            log.warning("stats write failed: %s", e)

    def flush(self) -> None:
        """Wait for queued writes (tests, shutdown)."""
        self.exec.submit(lambda: None).result()

    def close(self) -> None:
        """Flush and release the file (Windows keeps an open SQLite file locked)."""
        self.exec.shutdown(wait=True)
        with self.lock:
            self.db.close()

    def summary(self, hours: float, source: str | None = None, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        sql = "SELECT ts, source, lang, llm, actions, metrics FROM turns WHERE ts >= ?"
        args: list = [now - hours * 3600]
        if source:
            sql += " AND source = ?"
            args.append(source)
        with self.lock:
            rows = self.db.execute(sql + " ORDER BY ts", args).fetchall()

        turns = [{"ts": ts, "source": src, "lang": lang, "llm": llm, "actions": n, "metrics": json.loads(m)}
                 for ts, src, lang, llm, n, m in rows]
        by_day: dict[str, list[dict]] = {}
        for t in turns:
            by_day.setdefault(time.strftime("%Y-%m-%d", time.localtime(t["ts"])), []).append(t)

        def values(ts: list[dict], key: str) -> list[float]:
            return [t["metrics"][key] for t in ts if isinstance(t["metrics"].get(key), int | float)]

        return {
            "hours": hours,
            "source": source,
            "turns": len(turns),
            "by_source": {s: sum(t["source"] == s for t in turns) for s in SOURCES},
            "by_llm": {name: sum(t["llm"] == name for t in turns) for name in sorted({t["llm"] for t in turns})},
            "with_actions": sum(t["actions"] > 0 for t in turns),
            "metrics": {k: describe(values(turns, k)) for k in METRICS},
            "daily": [{"day": day, "turns": len(ts), "first_audio_ms": describe(values(ts, "first_audio_ms"))}
                      for day, ts in sorted(by_day.items())],
            "recent": turns[-RECENT:][::-1],
        }
