"""Start the server: ``uv run python -m server``.

One uvicorn server (one lifespan, one set of models) on two loopback sockets: PORT for this
computer's browser and, with REMOTE_ENABLED, REMOTE_PORT for ``tailscale serve``. Refuses to start
on a non-loopback address or with an incomplete remote configuration.
"""

from __future__ import annotations

import ipaddress
import socket
import sys

import uvicorn

from .config import cfg

MIN_TOKEN_CHARS = 32
MIN_PIN_DIGITS = 6


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def config_problems() -> list[str]:
    problems = []
    if not is_loopback(cfg.host):
        problems.append(f"HOST={cfg.host} is not loopback: never expose the server directly, use `tailscale serve`")
    pin = cfg.api_confirm_pin
    if cfg.api_allow_risky and not (pin.isdigit() and len(pin) >= MIN_PIN_DIGITS):
        problems.append(f"API_ALLOW_RISKY=true needs API_CONFIRM_PIN with >= {MIN_PIN_DIGITS} digits")
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
    try:
        # POSIX: only skips TIME_WAIT. On Windows SO_REUSEADDR would let two servers share the port,
        # so set nothing there, like asyncio does.
        if sys.platform != "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((socket.gethostbyname(host), port))
    except OSError:
        sock.close()
        raise
    return sock


def bind_all(host: str, ports: list[int]) -> list[socket.socket] | None:
    """Bind every port, or close what was opened, explain and return None."""
    sockets: list[socket.socket] = []
    for port in ports:
        try:
            sockets.append(bind(host, port))
        except OSError as e:
            for s in sockets:
                s.close()
            hint = " (Windows: `netsh int ipv4 show excludedportrange protocol=tcp`)" if sys.platform == "win32" else ""
            print(f"Cannot listen on {host}:{port}: {e}\nThe port is in use (is the server already running?) or "
                  f"reserved{hint}. Choose another with PORT / REMOTE_PORT in .env.", file=sys.stderr)
            return None
    return sockets


def main() -> int:
    if sys.platform == "win32":  # a redirected console is cp1252: never crash printing a transcript
        for stream in (sys.stdout, sys.stderr):
            stream.reconfigure(errors="backslashreplace")
    if problems := config_problems():
        print("Not starting:\n- " + "\n- ".join(problems), file=sys.stderr)
        return 2
    sockets = bind_all(cfg.host, [cfg.port, *([cfg.remote_port] if cfg.remote_enabled else [])])
    if sockets is None:
        return 2
    print(f"Local:  http://{cfg.host}:{cfg.port}"
          + (f"\nRemote: https://{cfg.public_host} (tailscale serve --bg {cfg.remote_port})" if cfg.remote_enabled else ""))
    from .main import app

    uvicorn.Server(uvicorn.Config(app, log_level="info")).run(sockets=sockets)
    return 0


if __name__ == "__main__":
    sys.exit(main())
