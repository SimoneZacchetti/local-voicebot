"""Home Assistant client: MCP server tools (Assist API) plus a few REST extras.

HA's MCP server (/api/mcp) is stateless streamable HTTP: plain JSON-RPC POSTs, answered
with JSON (or a short SSE stream). A tiny client is enough, no MCP SDK needed.
"""

from __future__ import annotations

import itertools
import json
import logging
from typing import Any

import httpx

from .config import cfg

log = logging.getLogger("voicebot.ha")


class HomeAssistant:
    def __init__(self, url: str | None = None, token: str | None = None) -> None:
        self.url = (url or cfg.ha_url).rstrip("/")
        self.client = httpx.AsyncClient(
            base_url=self.url,
            headers={"Authorization": f"Bearer {token or cfg.ha_token}"},
            timeout=httpx.Timeout(cfg.tool_timeout_s, connect=3.0),
        )
        self._ids = itertools.count(1)
        self.services: set[str] = set()  # "domain.service" available in HA

    # -- MCP --------------------------------------------------------------------------

    async def _rpc(self, method: str, params: dict | None = None) -> Any:
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params is not None:
            body["params"] = params
        r = await self.client.post(
            cfg.ha_mcp_path, json=body,
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
        )
        r.raise_for_status()
        if r.headers.get("content-type", "").startswith("text/event-stream"):
            data = [ln[5:].strip() for ln in r.text.splitlines() if ln.startswith("data:")]
            msg = json.loads(data[-1])
        else:
            msg = r.json()
        if "error" in msg:
            raise RuntimeError(f"MCP {method}: {msg['error']}")
        return msg["result"]

    async def list_tools(self) -> list[dict]:
        await self._rpc("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "voicebot", "version": "0.1"},
        })
        return (await self._rpc("tools/list"))["tools"]

    async def home_prompt(self) -> str:
        """HA's own Assist prompt: usage rules plus a static index of areas and devices."""
        res = await self._rpc("prompts/get", {"name": "Assist"})
        return "\n".join(m["content"].get("text", "") for m in res.get("messages", []))

    async def call_tool(self, name: str, args: dict) -> dict:
        res = await self._rpc("tools/call", {"name": name, "arguments": args})
        text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        return {"ok": not res.get("isError", False), "result": text}

    # -- REST ---------------------------------------------------------------------------

    async def load_services(self) -> None:
        r = await self.client.get("/api/services")
        r.raise_for_status()
        self.services = {f"{d['domain']}.{s}" for d in r.json() for s in d.get("services", {})}

    async def call_service(self, domain: str, service: str, data: dict) -> dict:
        r = await self.client.post(f"/api/services/{domain}/{service}", json=data)
        if r.status_code >= 400:
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        return {"ok": True}

    async def states(self) -> list[dict]:
        r = await self.client.get("/api/states")
        r.raise_for_status()
        return r.json()

    async def entities(self, domain: str) -> list[dict]:
        return [s for s in await self.states() if s["entity_id"].startswith(domain + ".")]

    async def aclose(self) -> None:
        await self.client.aclose()
