"""Start the server: ``uv run python -m server``.

One uvicorn server (one lifespan, one set of models) on two loopback sockets: PORT for the
Mac's browser and, with REMOTE_ENABLED, REMOTE_PORT for ``tailscale serve``. Refuses to start
on a non-loopback address or with an incomplete remote configuration.
"""

from __future__ import annotations

import ipaddress
import socket
import sys

import uvicorn

from .config import cfg

MIN_TOKEN_CHARS = 32


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def config_problems() -> list[str]:
    problems = []
    if not is_loopback(cfg.host):
        problems.append(f"HOST={cfg.host} is not loopback: never expose the server directly, use `tailscale serve`")
    if cfg.remote_enabled:
        if cfg.remote_port == cfg.port:
            problems.append("REMOTE_PORT must differ from PORT")
        if not cfg.public_host:
            problems.append("PUBLIC_HOST is empty (e.g. voicebot-mac.tailXXXX.ts.net)")
        if not cfg.ts_allowed_logins:
            problems.append("TS_ALLOWED_LOGINS is empty: nobody could use the remote port")
        if any(len(t) < MIN_TOKEN_CHARS for t in cfg.api_tokens):
            problems.append(f"every API_TOKENS entry needs >= {MIN_TOKEN_CHARS} chars (scripts/gen_secrets.py)")
    return problems


def bind(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((socket.gethostbyname(host), port))
    return sock


def main() -> int:
    if problems := config_problems():
        print("Not starting:\n- " + "\n- ".join(problems), file=sys.stderr)
        return 2
    sockets = [bind(cfg.host, cfg.port)]
    if cfg.remote_enabled:
        sockets.append(bind(cfg.host, cfg.remote_port))
    print(f"Local:  http://{cfg.host}:{cfg.port}"
          + (f"\nRemote: https://{cfg.public_host} (tailscale serve --bg {cfg.remote_port})" if cfg.remote_enabled else ""))
    from .main import app

    uvicorn.Server(uvicorn.Config(app, log_level="info")).run(sockets=sockets)
    return 0


if __name__ == "__main__":
    sys.exit(main())
