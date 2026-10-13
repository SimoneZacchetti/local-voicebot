"""One-shot commands from the phone (Siri Shortcuts, widgets): POST /api/command.

Each command runs a full typed turn (same LLM, tools and safety as voice) in a headless
conversation kept for a while, so "e in camera?" after "accendi la luce in cucina" works.
Risky actions (locks, doors, alarms) never run from a single request: by default they are
refused ("blocked"); with API_ALLOW_RISKY they need a second call, /api/confirm, with the
PIN, before the confirmation expires. A "sì" sent to /api/command never confirms anything.
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
import time
from dataclasses import dataclass, field

from .config import cfg
from .session import HeadlessSession, Services, Turn

DEFAULT_ID = "phone"
MAX_SESSIONS = 8
BLOCKED = "For locks, doors and alarms use the app: I can't confirm that from here."
EXPIRED = "Request expired, say the command again."


@dataclass
class PhoneSession:
    session: HeadlessSession
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # one command at a time (not session.lock)
    turn: Turn | None = None
    last_used: float = field(default_factory=time.monotonic)


class ConfirmError(Exception):
    def __init__(self, status: int, body: dict) -> None:
        super().__init__(body.get("reply", ""))
        self.status, self.body = status, body


class PhoneSessions:
    def __init__(self, svc: Services) -> None:
        self.svc = svc
        self.sessions: dict[str, PhoneSession] = {}

    def _get(self, sid: str | None) -> tuple[str, PhoneSession]:
        now = time.monotonic()
        for k in [k for k, v in self.sessions.items() if now - v.last_used > cfg.phone_session_ttl_s and not self._busy(v)]:
            del self.sessions[k]
        sid = sid or DEFAULT_ID
        entry = self.sessions.get(sid)
        if entry is None:
            if sid != DEFAULT_ID and len(sid) < 16:
                sid = secrets.token_urlsafe(16)  # unknown short id: never let callers pick guessable ids
            while len(self.sessions) >= MAX_SESSIONS:
                oldest = min(self.sessions, key=lambda k: self.sessions[k].last_used)
                del self.sessions[oldest]
            entry = self.sessions[sid] = PhoneSession(HeadlessSession(self.svc))
            entry.session.confirm_ttl_s = cfg.api_confirm_ttl_s
        entry.last_used = now
        return sid, entry

    @staticmethod
    def _busy(entry: PhoneSession) -> bool:
        return entry.turn is not None and not entry.turn.done

    async def command(self, text: str, sid: str | None = None) -> dict:
        sid, entry = self._get(sid)
        if self._busy(entry):
            return {"status": "running", "reply": "Sto ancora lavorando alla richiesta precedente.", "session_id": sid}
        async with entry.lock:
            s = entry.session
            s.pending = None  # a reply sent here is never the answer to "are you sure?"
            async with s.lock:
                turn = s.run_text(text)
            if turn is None:
                return {"status": "error", "reply": "Messaggio vuoto.", "session_id": sid}
            entry.turn = turn
            return await self._result(sid, entry, turn)

    async def confirm(self, sid: str, yes: bool, pin: str = "") -> dict:
        entry = self.sessions.get(sid)
        p = entry.session.pending if entry else None
        if entry is None or p is None or not p.asked or time.monotonic() > p.expires_at:
            if entry is not None:
                entry.session.pending = None
            raise ConfirmError(410, {"status": "expired", "reply": EXPIRED, "session_id": sid})
        if cfg.api_confirm_pin and not hmac.compare_digest(str(pin).encode(), cfg.api_confirm_pin.encode()):
            raise ConfirmError(403, {"status": "forbidden", "reply": "PIN sbagliato.", "session_id": sid})
        async with entry.lock:
            s = entry.session
            async with s.lock:
                turn = s.run_text("sì" if yes else "no")  # same deterministic path as voice
            entry.turn = turn
            return await self._result(sid, entry, turn)

    async def _result(self, sid: str, entry: PhoneSession, turn: Turn) -> dict:
        s = entry.session
        try:
            await asyncio.wait_for(asyncio.shield(turn.task), cfg.api_timeout_s)
        except TimeoutError:
            # Not cancelled: the house may already be acting. The next command says "running".
            return {"status": "running", "reply": "Ci sto lavorando, ci vuole un po'.", "session_id": sid}
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - logged by the session task callback
            return {"status": "error", "reply": "Something went wrong.", "session_id": sid}
        body = {
            "status": "done",
            "reply": " ".join(turn.sentences),
            "session_id": sid,
            "actions": turn.actions,
            "route": self.svc.tools.mode,
        }
        p = s.pending
        if p is not None and p.turn_id == turn.id:
            if not cfg.api_allow_risky:
                s.pending = None
                return {**body, "status": "blocked", "reply": BLOCKED}
            body.update(status="needs_confirmation", reply=p.question, question=p.question,
                        expires_in=round(p.expires_at - time.monotonic()))
        return body
