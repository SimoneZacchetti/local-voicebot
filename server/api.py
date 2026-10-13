"""HTTP API for the phone (Siri Shortcuts, widgets). Every route needs a Bearer token
(API_TOKENS); on the remote port RemoteGate also requires an allowed Tailscale login."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .auth import require_token
from .config import cfg
from .phone import ConfirmError
from .session import MAX_TEXT_CHARS

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


class Command(BaseModel):
    text: str = Field(max_length=MAX_TEXT_CHARS)
    session_id: str | None = Field(default=None, max_length=64)


class Confirm(BaseModel):
    session_id: str = Field(max_length=64)
    yes: bool
    pin: str = Field(default="", max_length=32)


@router.post("/command")
async def command(body: Command, request: Request) -> dict:
    return await request.app.state.phone.command(body.text, body.session_id)


@router.post("/confirm")
async def confirm(body: Confirm, request: Request):
    try:
        return await request.app.state.phone.confirm(body.session_id, body.yes, body.pin)
    except ConfirmError as e:
        return JSONResponse(e.body, status_code=e.status)


@router.get("/status")
async def status(request: Request) -> dict:
    svc = request.app.state.svc
    warnings = [] if svc.tools.enabled or cfg.home_backend != "ha" else ["Home Assistant unreachable"]
    return {
        "route": svc.tools.mode,
        "home_tools": len(svc.tools.specs),
        "llm": svc.llm.name,
        "llm_model": cfg.groq_llm_model if svc.llm.name == "groq" else cfg.llm_model,
        "stt": svc.stt.name,
        "warnings": warnings,
    }
