"""Phone API (/api/command, /api/confirm): same turn pipeline and safety as voice."""

import time

import pytest

from server import main
from server.config import cfg

from .test_tools import home  # noqa: F401 - fixture

TOKEN = "k" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def api(home, monkeypatch):  # noqa: F811
    monkeypatch.setattr(cfg, "api_tokens", [TOKEN])
    monkeypatch.setattr(cfg, "api_allow_risky", False)
    monkeypatch.setattr(cfg, "api_confirm_pin", "")
    return home


def cmd(c, text, **kw):
    return c.post("/api/command", json={"text": text, **kw}, headers=AUTH)


def test_api_needs_a_valid_token(api, monkeypatch):
    assert api.post("/api/command", json={"text": "ciao"}).status_code == 401
    assert api.post("/api/command", json={"text": "ciao"}, headers={"Authorization": "Bearer nope"}).status_code == 401
    monkeypatch.setattr(cfg, "api_tokens", [])
    assert cmd(api, "ciao").status_code == 503


def test_command_runs_the_tool_and_replies(api):
    r = cmd(api, "spegni la luce della cucina").json()
    assert r["status"] == "done" and r["reply"] == "Fatto, luce spenta."
    assert r["actions"] and r["session_id"] == "phone"
    assert api.home.calls == [("intent__HassTurnOff", {"name": "Luce cucina"})]


def test_same_session_keeps_history(api):
    cmd(api, "spegni la luce della cucina")
    cmd(api, "e adesso cosa fai?")
    history = main.app.state.phone.sessions["phone"].session.history
    assert history[0]["content"] == "spegni la luce della cucina"
    assert history[-2]["content"] == "e adesso cosa fai?"


def test_risky_action_is_blocked_by_default(api):
    r = cmd(api, "sblocca la porta di casa").json()
    assert r["status"] == "blocked" and "app" in r["reply"]
    assert api.home.calls == []
    assert main.app.state.phone.sessions["phone"].session.pending is None
    # "sì" afterwards is a new request, never a confirmation
    cmd(api, "sì, confermo")
    assert api.home.calls == []


def test_risky_action_with_confirmation_and_pin(api, monkeypatch):
    monkeypatch.setattr(cfg, "api_allow_risky", True)
    monkeypatch.setattr(cfg, "api_confirm_pin", "2468")
    r = cmd(api, "sblocca la porta di casa").json()
    assert r["status"] == "needs_confirmation" and r["question"].startswith("Vuoi davvero")
    assert 0 < r["expires_in"] <= cfg.api_confirm_ttl_s
    sid = r["session_id"]
    assert api.post("/api/confirm", json={"session_id": sid, "yes": True, "pin": "0000"}, headers=AUTH).status_code == 403
    assert api.home.calls == []
    r = api.post("/api/confirm", json={"session_id": sid, "yes": True, "pin": "2468"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "done"
    assert api.home.calls == [("intent__HassTurnOff", {"name": "Porta di casa"})]
    # Already used: a second confirmation finds nothing to confirm
    r = api.post("/api/confirm", json={"session_id": sid, "yes": True, "pin": "2468"}, headers=AUTH)
    assert r.status_code == 410 and len(api.home.calls) == 1


def test_risky_action_refused(api, monkeypatch):
    monkeypatch.setattr(cfg, "api_allow_risky", True)
    sid = cmd(api, "sblocca la porta di casa").json()["session_id"]
    r = api.post("/api/confirm", json={"session_id": sid, "yes": False}, headers=AUTH).json()
    assert r["reply"] == "Va bene, annullato." and api.home.calls == []


def test_expired_or_unknown_confirmation_never_reaches_the_llm(api, monkeypatch):
    monkeypatch.setattr(cfg, "api_allow_risky", True)
    monkeypatch.setattr(cfg, "api_confirm_ttl_s", 0.0)
    sid = cmd(api, "sblocca la porta di casa").json()["session_id"]
    n = len(api.llm.calls)
    r = api.post("/api/confirm", json={"session_id": sid, "yes": True}, headers=AUTH)
    assert r.status_code == 410 and r.json()["status"] == "expired"
    r = api.post("/api/confirm", json={"session_id": "nope", "yes": True}, headers=AUTH)
    assert r.status_code == 410
    assert len(api.llm.calls) == n and api.home.calls == []


def test_slow_turn_answers_running_without_cancelling(api, monkeypatch):
    monkeypatch.setattr(cfg, "api_timeout_s", 0.05)
    api.home.delay = 0.3
    r = cmd(api, "spegni la luce della cucina").json()
    assert r["status"] == "running"
    assert cmd(api, "e la camera?").json()["status"] == "running"  # previous turn not replaced
    entry = main.app.state.phone.sessions["phone"]
    for _ in range(100):  # the app's event loop runs in TestClient's thread
        if entry.turn.done:
            break
        time.sleep(0.02)
    assert entry.turn.done and api.home.calls == [("intent__HassTurnOff", {"name": "Luce cucina"})]


def test_status(api):
    r = api.get("/api/status", headers=AUTH).json()
    assert r["route"] == "home" and r["home_tools"] == 1 and r["warnings"] == []
    assert api.get("/api/status").status_code == 401


def test_empty_command(api):
    assert cmd(api, "  ...  ").json()["status"] == "error"
