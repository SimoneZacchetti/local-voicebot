"""Latency log (/stats): percentiles, summaries and recording from real turns."""

import pytest

from server import main
from server.stats import Stats, percentile

from .test_ws_protocol import client, recv, recv_until, utt  # noqa: F401 - fixture


def test_percentile_nearest_rank():
    assert percentile([], 50) is None
    assert percentile([7], 95) == 7
    values = list(range(1, 101))
    assert percentile(values, 50) == 50
    assert percentile(values, 95) == 95
    assert percentile(values, 100) == 100
    assert percentile([300, 100, 200], 50) == 200


def test_summary_windows_sources_and_days(tmp_path):
    stats = Stats(tmp_path / "s.db")
    now = 1_800_000_000.0
    rows = [  # (hours ago, source, first_audio_ms)
        (0.5, "voice", 400), (1.5, "voice", 600), (2, "chat", 900), (30, "chat", 5000),
    ]
    for ago, source, fa in rows:
        stats._insert((now - ago * 3600, source, "it", "ollama", 1 if fa == 5000 else 0,
                       f'{{"first_audio_ms": {fa}, "total_ms": {fa + 100}}}'))

    s = stats.summary(24, now=now)
    assert s["turns"] == 3
    assert s["by_source"] == {"voice": 2, "chat": 1}
    assert s["metrics"]["first_audio_ms"] == {"n": 3, "p50": 600, "p95": 900, "max": 900}
    assert s["metrics"]["stt_ms"]["n"] == 0
    assert s["recent"][0]["metrics"]["first_audio_ms"] == 400  # newest first

    week = stats.summary(168, now=now)
    assert week["turns"] == 4 and week["with_actions"] == 1
    assert sum(d["turns"] for d in week["daily"]) == 4

    assert stats.summary(24, source="voice", now=now)["turns"] == 2
    stats.close()


def test_old_rows_are_dropped_on_open(tmp_path):
    import time

    stats = Stats(tmp_path / "s.db")
    stats._insert((time.time() - 100 * 86400, "voice", "it", "ollama", 0, "{}"))
    stats._insert((time.time(), "voice", "it", "ollama", 0, "{}"))
    stats.close()
    reopened = Stats(tmp_path / "s.db", retention_days=90)
    assert reopened.summary(24 * 366)["turns"] == 1
    reopened.close()


@pytest.fixture
def stats(tmp_path):
    s = Stats(tmp_path / "stats.db")
    yield s
    s.close()


def test_voice_turn_is_recorded(client, stats):  # noqa: F811
    main.app.state.svc.stats = stats
    with client.websocket_connect("/ws") as ws:
        assert recv(ws)["type"] == "ready"
        ws.send_bytes(utt(1, 8000))
        recv_until(ws, lambda m: m["type"] == "turn_end")
    stats.flush()
    s = stats.summary(1)
    assert s["turns"] == 1 and s["by_source"]["voice"] == 1
    t = s["recent"][0]
    assert t["llm"] == "fake" and t["lang"] == "it"
    assert {"stt_ms", "first_audio_ms", "total_ms"} <= t["metrics"].keys()
    assert "Toscana" not in str(t)  # what the user said is never stored


def test_greeting_is_not_recorded(client, stats):  # noqa: F811
    main.app.state.svc.stats = stats
    with client.websocket_connect("/ws") as ws:
        assert recv(ws)["type"] == "ready"
        ws.send_json({"type": "hello"})
        recv_until(ws, lambda m: m["type"] == "turn_end")
    stats.flush()
    assert stats.summary(1)["turns"] == 0


def test_stats_endpoints(client, stats):  # noqa: F811
    assert client.get("/stats.json").json() == {"enabled": False}
    main.app.state.svc.stats = stats
    r = client.get("/stats.json", params={"hours": 168, "source": "nope"}).json()
    assert r["enabled"] is True and r["hours"] == 168 and r["source"] is None and r["turns"] == 0
    assert client.get("/stats.json", params={"hours": 0}).status_code == 422
    page = client.get("/stats")
    assert page.status_code == 200 and "stats.json" in page.text



def test_close_flushes_and_releases_the_file(tmp_path):
    stats = Stats(tmp_path / "s.db")
    stats.record("chat", "it", "ollama", 0, {"total_ms": 5})
    stats.close()
    stats.record("chat", "it", "ollama", 0, {})  # after shutdown: dropped, never raises
    (tmp_path / "s.db").rename(tmp_path / "moved.db")  # Windows refuses this while the file is open
    reopened = Stats(tmp_path / "moved.db")
    assert reopened.summary(1)["turns"] == 1
    reopened.close()
