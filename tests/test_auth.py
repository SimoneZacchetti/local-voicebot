"""Access rules for the local and remote (tailnet) ports: server/auth.py."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server import __main__ as entry
from server import main
from server.auth import ts_login
from server.config import cfg

from .test_ws_protocol import client, recv, recv_until  # noqa: F401 - fixture

HOST = "voicebot-mac.tail1234.ts.net"
ORIGIN = f"https://{HOST}"
ME = "me@example.com"
TOKEN = "t" * 40


@pytest.fixture
def remote(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(cfg, "remote_enabled", True)
    monkeypatch.setattr(cfg, "remote_port", 8001)
    monkeypatch.setattr(cfg, "public_host", HOST)
    monkeypatch.setattr(cfg, "ts_allowed_logins", [ME])
    monkeypatch.setattr(cfg, "api_tokens", [TOKEN])
    # Same app and services as `client`, reached on the remote port (Host ends with :8001).
    c = TestClient(main.app, base_url=f"http://{HOST}:8001")
    c.sessions = client.sessions
    return c


def ws_url(host=HOST, port=8001):
    return f"ws://{host}:{port}/ws"


def refused(c, url, headers):
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect(url, headers=headers) as ws:
            ws.receive_json()
    return e.value.code


# -- remote port --------------------------------------------------------------------------


def test_remote_page_needs_allowed_login(remote):
    assert remote.get("/", headers={"Tailscale-User-Login": ME}).status_code == 200
    assert remote.get("/").status_code == 403
    assert remote.get("/", headers={"Tailscale-User-Login": "intruder@example.com"}).status_code == 403


def test_remote_login_header_is_rfc2047_decoded():
    assert ts_login({"tailscale-user-login": "=?utf-8?q?M=C3=A9@example.com?="}) == "mé@example.com"


def test_remote_wrong_host_is_refused(remote):
    r = remote.get("/", headers={"Tailscale-User-Login": ME, "Host": "evil.example"})
    assert r.status_code == 403


def test_remote_disabled_refuses_everything(remote, monkeypatch):
    monkeypatch.setattr(cfg, "remote_enabled", False)
    assert remote.get("/", headers={"Tailscale-User-Login": ME}).status_code == 403


def test_healthz_is_open(remote):
    assert remote.get("/healthz").status_code == 200


def test_remote_ws_with_exact_origin(remote):
    with remote.websocket_connect(ws_url(), headers={"Tailscale-User-Login": ME, "Origin": ORIGIN}) as ws:
        assert recv(ws)["type"] == "ready"


def test_remote_ws_with_bearer_and_no_origin(remote):
    headers = {"Tailscale-User-Login": ME, "Authorization": f"Bearer {TOKEN}"}
    with remote.websocket_connect(ws_url(), headers=headers) as ws:
        assert recv(ws)["type"] == "ready"


@pytest.mark.parametrize("headers", [
    {"Tailscale-User-Login": ME, "Origin": "https://evil.example"},
    {"Tailscale-User-Login": ME},  # no origin, no token
    {"Tailscale-User-Login": ME, "Authorization": "Bearer wrong"},
    {"Origin": ORIGIN},  # no login
    {"Tailscale-User-Login": ME, "Origin": "https://evil.example", "Authorization": f"Bearer {TOKEN}"},
])
def test_remote_ws_refused(remote, headers):
    assert refused(remote, ws_url(), headers) == 1008
    assert remote.sessions == []


def test_remote_post_needs_origin_or_token(remote):
    login = {"Tailscale-User-Login": ME}
    assert remote.post("/healthz-nope", headers=login).status_code == 403
    assert remote.post("/healthz-nope", headers={**login, "Origin": ORIGIN}).status_code != 403
    assert remote.post("/healthz-nope", headers={**login, "Authorization": f"Bearer {TOKEN}"}).status_code != 403


# -- local port ---------------------------------------------------------------------------


def test_local_ws_refuses_foreign_origin(client):  # noqa: F811
    assert refused(client, "/ws", {"Origin": "https://evil.example"}) == 1008
    assert client.sessions == []


@pytest.mark.parametrize("origin", [None, "http://127.0.0.1:8000"])
def test_local_ws_accepts_own_origin_or_none(client, origin):  # noqa: F811
    headers = {"Origin": origin} if origin else {}
    with client.websocket_connect("/ws", headers=headers) as ws:
        assert recv(ws)["type"] == "ready"


def test_local_dns_rebinding_is_refused(client):  # noqa: F811
    assert client.get("/", headers={"Host": "evil.example:8000"}).status_code == 403
    assert client.get("/", headers={"Host": "127.0.0.1:8000"}).status_code == 200


# -- protocol robustness ------------------------------------------------------------------


def test_malformed_messages_do_not_close_the_session(client):  # noqa: F811
    with client.websocket_connect("/ws") as ws:
        assert recv(ws)["type"] == "ready"
        ws.send_text("{not json")
        assert recv(ws) == {"type": "error", "stage": "protocol", "msg": "invalid message"}
        ws.send_text('["a list"]')
        assert recv(ws)["stage"] == "protocol"
        ws.send_json({"type": "interrupt", "heard_turn": "x", "heard_seq": None})
        ws.send_json({"type": "text", "text": "ciao", "heard_turn": "garbage"})
        assert recv_until(ws, lambda m: m["type"] == "transcript")[-1]["text"] == "ciao"


def test_oversized_utterance_is_dropped(client):  # noqa: F811
    with client.websocket_connect("/ws") as ws:
        assert recv(ws)["type"] == "ready"
        ws.send_bytes(b"\x00" * (31 * 16000 * 2))
        assert recv(ws) == {"type": "error", "stage": "protocol", "msg": "utterance too long"}


# -- startup checks -----------------------------------------------------------------------


def test_startup_refuses_public_bind(monkeypatch):
    monkeypatch.setattr(cfg, "host", "0.0.0.0")
    monkeypatch.setattr(cfg, "remote_enabled", False)
    assert any("not loopback" in p for p in entry.config_problems())


def test_startup_refuses_incomplete_remote_config(monkeypatch):
    monkeypatch.setattr(cfg, "host", "127.0.0.1")
    monkeypatch.setattr(cfg, "remote_enabled", True)
    monkeypatch.setattr(cfg, "public_host", "")
    monkeypatch.setattr(cfg, "ts_allowed_logins", [])
    monkeypatch.setattr(cfg, "api_tokens", ["short"])
    assert len(entry.config_problems()) == 3
    monkeypatch.setattr(cfg, "public_host", HOST)
    monkeypatch.setattr(cfg, "ts_allowed_logins", [ME])
    monkeypatch.setattr(cfg, "api_tokens", [TOKEN])
    assert entry.config_problems() == []


def test_public_host_with_custom_https_port(remote, monkeypatch):
    """`tailscale serve --https=9443`: Origin carries the port, Host is checked without it."""
    monkeypatch.setattr(cfg, "public_host", f"{HOST}:9443")
    headers = {"Tailscale-User-Login": ME, "Origin": f"{ORIGIN}:9443"}
    with remote.websocket_connect(ws_url(), headers=headers) as ws:
        assert recv(ws)["type"] == "ready"
    assert refused(remote, ws_url(), {"Tailscale-User-Login": ME, "Origin": ORIGIN}) == 1008
