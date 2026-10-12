"""Tools the LLM can call to act on the home, plus the safety policy around them.

Tools come from Home Assistant's MCP server (lights, climate, media, lists, live state,
exposed scripts...) plus two extras: Google Assistant text commands and announcements.
Risky actions (locks, doors, gates, alarms) are never executed on the LLM's word alone:
the user must answer "sì" to a spoken question, checked deterministically in code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from .config import cfg

log = logging.getLogger("voicebot.tools")


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class PendingAction:
    calls: list[ToolCall]
    lang: str
    question: str  # spoken question, already in the user's language
    expires_at: float
    turn_id: int = 0  # turn that asked
    asked: bool = False  # the question was actually spoken


GOOGLE_TOOL = {
    "type": "function",
    "function": {
        "name": "google_assistant",
        "description": (
            "Sends a spoken-style command to the user's Google Home / Google Assistant account, in the "
            "user's language (e.g. 'accendi la TV del salotto', 'avvia la routine buongiorno'). Use it ONLY "
            "for things the other tools cannot do. Google does not report the outcome."
        ),
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "The command, as the user would say it to Google"}},
            "required": ["command"],
        },
    },
}

# Away from home the local network (Cast, Matter, Music Assistant) is out of reach: every command
# goes through Google's cloud, which runs it on the devices at home.
GOOGLE_AWAY_TOOL = {
    "type": "function",
    "function": {
        "name": "google_assistant",
        "description": (
            "Controls the user's home through Google Assistant: lights, plugs, TV, speakers, music, thermostat, "
            "routines. Write the command in Italian as the user would say it to Google, with device and room "
            "names (e.g. 'accendi la luce della cucina', 'metti musica rilassante su Speaker Salotto con Spotify'). "
            "Google does not report the outcome: say the command was sent, not that it was done."
        ),
        "parameters": GOOGLE_TOOL["function"]["parameters"],
    },
}

AWAY_NOTE = ("\nThe user is AWAY from home right now: the device list above is for names only. Control every device "
             "with the google_assistant tool (one command per action); no other home tool is available.")

ANNOUNCE_TOOL = {
    "type": "function",
    "function": {
        "name": "announce",
        "description": (
            "Speaks a message out loud on the house speakers (Google/Nest speakers). Use it whenever the user asks to "
            "announce, broadcast, say or play a message on a speaker or in a room."
        ),
        "parameters": {
            "type": "object",
            "properties": {"message": {"type": "string", "description": "What to say, in the user's language"}},
            "required": ["message"],
        },
    },
}


# -- safety -----------------------------------------------------------------------------
#
# Fail closed: a device-control call runs without confirmation only when we can show it
# cannot touch a lock, door, gate, garage, alarm or valve. Anything unresolved is confirmed.

def norm(text: str) -> str:
    """Lowercase, no accents, no punctuation: for matching names said by voice."""
    t = unicodedata.normalize("NFKD", str(text).lower())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^\w\s]", " ", t).replace("_", " ").split())


_RISKY_WORDS = re.compile(
    r"serratur|lucchett|porta|portone|portoncino|cancell|garage|basculante|saracinesc|\bbox\b|allarm|antifurto|"
    r"valvol|\bgas\b|lock|door|gate|alarm|valve|portail|porte|serrure|tur\b|tor\b|schloss|alarmanlage|"
    r"puerta|porton|cerradura|alarma|portao|fechadura",
    re.IGNORECASE,
)
_RISKY_CLASSES = {"garage", "gate", "door"}
# Domains where HA cannot match a lock/door/alarm: a call restricted to these is safe.
_SAFE_DOMAINS = {"light", "fan", "media_player", "climate", "humidifier", "vacuum", "water_heater",
                 "scene", "script", "input_boolean", "todo"}
_DEVICE_INTENTS = {"HassTurnOn", "HassTurnOff", "HassSetPosition"}

# What an intent actually does, per domain. Note HA's lock semantics: TurnOn = lock, TurnOff = unlock.
_VERBS = {
    ("lock", "HassTurnOn"): ("chiudere a chiave", "lock"),
    ("lock", "HassTurnOff"): ("sbloccare", "unlock"),
    ("cover", "HassTurnOn"): ("aprire", "open"),
    ("cover", "HassTurnOff"): ("chiudere", "close"),
    ("cover", "HassSetPosition"): ("cambiare la posizione di", "change the position of"),
    ("valve", "HassTurnOn"): ("aprire", "open"),
    ("valve", "HassTurnOff"): ("chiudere", "close"),
    ("alarm_control_panel", "HassTurnOn"): ("attivare", "arm"),
    ("alarm_control_panel", "HassTurnOff"): ("disattivare", "disarm"),
    # Unknown target: name every effect the call could have.
    (None, "HassTurnOn"): ("accendere, aprire o chiudere a chiave", "turn on, open or lock"),
    (None, "HassTurnOff"): ("spegnere, chiudere o sbloccare", "turn off, close or unlock"),
    (None, "HassSetPosition"): ("cambiare la posizione di", "change the position of"),
}


@dataclass
class Entity:
    entity_id: str
    domain: str
    name: str
    device_class: str = ""

    @property
    def risky(self) -> bool:
        if self.domain in cfg.risky_domains:
            return True
        if self.domain == "cover" and self.device_class in _RISKY_CLASSES:
            return True
        # Relays and generic covers often drive gates/doors: judge them by name too.
        return self.domain in {"switch", "button", "cover", "input_boolean"} and bool(_RISKY_WORDS.search(norm(self.name)))


def _as_list(v) -> list[str]:
    return [v] if isinstance(v, str) else [str(x) for x in (v or [])]


def risk_of(call: ToolCall, lookup=lambda name: []) -> str | None:
    """If ``call`` needs a spoken confirmation, return the question ("italian|english").

    ``lookup(name)`` returns the Home Assistant entities with that name. The question is
    built from the call itself, never from the LLM's words, so what the user confirms is
    exactly what will run.
    """
    a = call.args
    intent = call.name.split("__")[-1]
    if call.name == "google_assistant":
        cmd = str(a.get("command", ""))
        if _RISKY_WORDS.search(norm(cmd)):
            return f"Vuoi davvero che chieda a Google: {cmd}?|Do you really want me to ask Google: {cmd}?"
        return None
    if intent not in _DEVICE_INTENTS:
        return None  # lights, climate, media, lists, live context...: cannot open or unlock
    domains = _as_list(a.get("domain"))
    classes = _as_list(a.get("device_class"))
    name = str(a.get("name") or "")
    target = name or str(a.get("area") or a.get("floor") or "")
    risky_domain = next((d for d in domains if d in cfg.risky_domains), None)
    if risky_domain is None and set(classes) & _RISKY_CLASSES:
        risky_domain = "cover"
    if risky_domain is None:
        if domains and set(domains) <= _SAFE_DOMAINS:
            return None  # HA only matches entities of these domains
        matches = [e for e in lookup(name) if not domains or e.domain in domains] if name else []
        if matches and not any(e.risky for e in matches):
            return None  # resolved to known, harmless entities
        risky = [e for e in matches if e.risky]
        # Name the real effect when the target is known; otherwise list every possible one.
        risky_domain = risky[0].domain if risky and len({e.domain for e in risky}) == 1 else None
        if risky_domain is None and len(domains) == 1 and (domains[0], intent) in _VERBS:
            risky_domain = domains[0]  # e.g. an unknown cover: "aprire"
    it, en = _VERBS.get((risky_domain, intent)) or _VERBS[(None, intent)]
    return f"Vuoi davvero {it} {target}?|Do you really want to {en} {target}?"


def confirmation_question(risks: list[str] | str, lang: str) -> str:
    """One spoken question for one or more risky actions."""
    risks = [risks] if isinstance(risks, str) else risks
    parts = [r.split("|", 1)[0 if lang == "it" else 1] for r in risks]
    if len(parts) == 1:
        return parts[0]
    # "Vuoi davvero sbloccare X?" + "Vuoi davvero aprire Y?" -> "Vuoi davvero sbloccare X e aprire Y?"
    prefix = "Vuoi davvero " if lang == "it" else "Do you really want to "
    joiner = " e " if lang == "it" else " and "
    body = [p.removeprefix(prefix).rstrip("?") for p in parts]
    return prefix + ", ".join(body[:-1]) + joiner + body[-1] + "?"


_YES_CORE = {"si", "certo", "certamente", "conferma", "confermo", "vai", "ok", "okay", "procedi", "fallo",
             "esatto", "yes", "yeah", "yep", "sure", "confirm", "oui", "ja", "sim", "bene", "do", "ahead"}
_YES_FILLER = {"va", "pure", "grazie", "per", "favore", "please", "go", "it", "thanks", "si", "allora", "dai"}
_NEG = {"no", "non", "not", "ma", "but", "aspetta", "wait", "annulla", "stop", "fermo", "ferma", "cancel",
        "nope", "lascia", "ancora", "dont", "don", "never", "mai", "nein", "non", "pas"}


def yes_no(text: str) -> bool | None:
    """Deterministic answer to a confirmation question.

    True only for a short, plain yes ("sì", "sì, confermo", "ok vai", "yes please").
    False for anything with a negation or hesitation ("no", "sì ma non aprire", "aspetta").
    None for everything else ("ok, e spegni la tv"): the action is dropped, not run.
    """
    words = norm(text).replace("don t", "dont").split()
    if not words:
        return None
    if any(w in _NEG for w in words):
        return False
    if len(words) <= 5 and all(w in _YES_CORE or w in _YES_FILLER for w in words) and any(w in _YES_CORE for w in words):
        return True
    return None


# -- registry -----------------------------------------------------------------------------


class ToolRegistry:
    def __init__(self, ha=None) -> None:
        self.ha = ha
        self.specs: list[dict] = []
        self._mcp_names: set[str] = set()
        self.announce_players: list[str] = []
        self.tts_entity: str | None = None
        self.context = ""  # device index for the system prompt (fixed per process: prompt cache)
        self.entities: dict[str, list[Entity]] = {}  # normalized name -> entities (safety checks)
        self.mode = "home"  # "home": Home Assistant tools; "away": Google Assistant only (see HOME_ROUTE)
        self._home: tuple[list[dict], str] = ([], "")
        self._probe_at = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.specs)

    async def start(self) -> bool:
        """Discover tools. Never raises: without Home Assistant the bot just has no tools.

        Returns False when Home Assistant is configured but unreachable, so the caller can retry
        (e.g. the Tailscale VPN comes up after the server)."""
        if self.ha is None:
            return True
        try:
            tools = await self.ha.list_tools()
            await self.ha.load_services()
            self.context = (await self.ha.home_prompt())[: cfg.home_context_max_chars]
            if cfg.ha_default_player:
                self.context += (f"\nWhen the user asks for music, media or volume without saying where, use the media "
                                 f"player named \"{cfg.ha_default_player}\" (pass it as name).")
            self.index_entities(await self.ha.states())  # refreshed below too
        except Exception as e:  # noqa: BLE001
            log.error("Home Assistant not reachable at %s: %s (home control disabled until it is)", self.ha.url, e)
            self.context, self.entities = "", {}
            return False
        allow = set(cfg.ha_tool_allowlist)
        states = await self.ha.states()
        has_satellites = any(st["entity_id"].startswith("assist_satellite.") for st in states)
        for t in tools:
            if t["name"].endswith("HassBroadcast") and not has_satellites:
                continue  # HA's broadcast only reaches its own voice satellites: use "announce" instead
            if allow and t["name"] not in allow and t["name"].split("__")[-1] not in allow:
                continue
            self._mcp_names.add(t["name"])
            self.specs.append({"type": "function", "function": {
                "name": t["name"], "description": t.get("description", "")[:400],
                "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
            }})
        if cfg.google_fallback and "google_assistant_sdk.send_text_command" in self.ha.services:
            self.specs.append(GOOGLE_TOOL)
        if cfg.ha_announce_players and "tts.speak" in self.ha.services:
            ttss = await self.ha.entities("tts")
            if ttss:
                self.tts_entity = cfg.ha_tts_entity or ttss[0]["entity_id"]
                self.announce_players = cfg.ha_announce_players
        if self.tts_entity or "notify.google_assistant_sdk" in self.ha.services:
            self.specs.append(ANNOUNCE_TOOL)
        log.info("home tools: %s", ", ".join(s["function"]["name"] for s in self.specs))
        self._home = (self.specs, self.context)
        self.mode = "home"
        if cfg.home_route != "ha" and "google_assistant_sdk.send_text_command" not in self.ha.services:
            log.error("HOME_ROUTE=%s needs the Google Assistant SDK integration in Home Assistant: using local tools",
                      cfg.home_route)
        if cfg.home_route == "auto" and not cfg.home_probe:
            log.error("HOME_ROUTE=auto needs HOME_PROBE (host:port of a device at home): using local tools")
        return True

    @property
    def can_go_away(self) -> bool:
        if cfg.home_route == "auto" and not cfg.home_probe:
            return False  # nothing tells home from away
        return cfg.home_route != "ha" and self.ha is not None and "google_assistant_sdk.send_text_command" in self.ha.services

    async def update_route(self) -> bool:
        """Pick local or Google tools (HOME_ROUTE). Returns True when the tool set changed."""
        if not self.can_go_away:
            return False
        if cfg.home_route == "google":
            away = True
        else:  # auto: at home when a device on the home LAN answers
            if time.monotonic() < self._probe_at:
                return False
            self._probe_at = time.monotonic() + cfg.home_probe_ttl_s
            away = not await probe(cfg.home_probe)
        mode = "away" if away else "home"
        if mode == self.mode and self.specs:
            return False
        self.mode = mode
        specs, context = self._home
        if away:
            self.specs = [GOOGLE_AWAY_TOOL] + ([ANNOUNCE_TOOL] if "notify.google_assistant_sdk" in self.ha.services else [])
            self.context = context + AWAY_NOTE
        else:
            self.specs, self.context = specs, context
        log.info("home route: %s (%s)", mode, ", ".join(s["function"]["name"] for s in self.specs))
        return True

    async def execute(self, call: ToolCall) -> dict:
        try:
            if call.name in self._mcp_names:
                res = await self.ha.call_tool(call.name, call.args)
            elif call.name == "google_assistant" and self._has("google_assistant"):
                res = await self.ha.call_service("google_assistant_sdk", "send_text_command",
                                                 {"command": str(call.args.get("command", ""))})
                if res["ok"]:
                    res["note"] = ("sent to Google, which does NOT report the outcome: tell the user the command was sent "
                                   "(e.g. \"Ho chiesto a Google di …\"), never that it was done")
            elif call.name == "announce" and self._has("announce"):
                msg = str(call.args.get("message", ""))
                if self.tts_entity and self.mode == "home":
                    res = await self.ha.call_service("tts", "speak", {
                        "entity_id": self.tts_entity, "media_player_entity_id": self.announce_players, "message": msg})
                else:
                    res = await self.ha.call_service("notify", "google_assistant_sdk", {"message": msg})
            else:
                res = {"ok": False, "error": f"unknown tool {call.name}"}
        except Exception as e:  # noqa: BLE001 - errors become results the LLM can talk about
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        if "result" in res and len(res["result"]) > cfg.tool_result_max_chars:
            res["result"] = res["result"][: cfg.tool_result_max_chars] + "…"
        return res

    def _has(self, name: str) -> bool:
        return any(s["function"]["name"] == name for s in self.specs)

    def risk(self, call: ToolCall) -> str | None:
        if not cfg.confirm_risky:
            return None
        q = risk_of(call, self.lookup)
        if q is None and call.name == "google_assistant":
            # "apri il Nuki" says nothing risky in words: check the device names too.
            cmd = f" {norm(call.args.get('command', ''))} "
            if any(f" {name} " in cmd for name, ents in self.entities.items() if any(e.risky for e in ents)):
                c = call.args.get("command", "")
                q = f"Vuoi davvero che chieda a Google: {c}?|Do you really want me to ask Google: {c}?"
        return q

    def lookup(self, name: str) -> list[Entity]:
        """Home Assistant entities answering to ``name`` (friendly name or object id)."""
        return self.entities.get(norm(name), [])

    def index_entities(self, states: list[dict]) -> None:
        self.entities = {}
        for st in states:
            eid = st["entity_id"]
            attrs = st.get("attributes") or {}
            e = Entity(eid, eid.split(".", 1)[0], attrs.get("friendly_name") or eid, attrs.get("device_class") or "")
            for key in {norm(e.name), norm(eid.split(".", 1)[1])}:
                self.entities.setdefault(key, []).append(e)

    @staticmethod
    def summary(call: ToolCall) -> str:
        return f"{call.name.split('__')[-1]}({json.dumps(call.args, ensure_ascii=False)})"


async def probe(target: str, timeout: float = 0.8) -> bool:
    """True if ``host:port`` accepts a TCP connection (e.g. a Google Home on the home LAN, port 8009)."""
    host, _, port = target.rpartition(":")
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout)
    except (OSError, ValueError, asyncio.TimeoutError):
        return False
    w.close()
    return True


def new_pending(calls: list[ToolCall], lang: str, question: str, turn_id: int, ttl: float | None = None) -> PendingAction:
    return PendingAction(calls, lang, question, time.monotonic() + (cfg.confirm_ttl_s if ttl is None else ttl), turn_id)
