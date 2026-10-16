"""Home control: tool-call parsing, safety policy, and the tool loop inside a turn."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from server import main
from server.config import cfg
from server.langid import LangId
from server.llm import GroqLlm, OllamaLlm, to_ollama, to_openai
from server.session import Services, Session
from server.tools import ToolCall, ToolRegistry, confirmation_question, risk_of, yes_no

from .test_ws_protocol import UTTERANCES, FakeStt, FakeTts, recv, recv_until, utt

# -- parsing ------------------------------------------------------------------------------

NDJSON = [
    {"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_a", "function": {"index": 0, "name": "climate__HassClimateSetTemperature", "arguments": {"temperature": 21}}}]}, "done": False},
    {"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_b", "function": {"index": 1, "name": "intent__HassTurnOff", "arguments": {"name": "Luce cucina"}}}]}, "done": False},
    {"message": {"role": "assistant", "content": ""}, "done": True},
]


async def test_ollama_stream_yields_tool_calls_from_non_final_chunks():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text="\n".join(json.dumps(x) for x in NDJSON))

    llm = OllamaLlm()
    llm.client = httpx.AsyncClient(base_url="http://x", transport=httpx.MockTransport(handler))
    tools = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
    out = [d async for d in llm.stream([{"role": "user", "content": "hi"}], tools)]
    assert out == [
        ToolCall("call_a", "climate__HassClimateSetTemperature", {"temperature": 21}),
        ToolCall("call_b", "intent__HassTurnOff", {"name": "Luce cucina"}),
    ]
    assert seen["body"]["tools"] == tools and seen["body"]["think"] is False


def test_message_conversion():
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "f", "args": {"x": 1}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "f", "content": "{\"ok\": true}"},
    ]
    assert to_ollama(msgs) == [
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "f", "arguments": {"x": 1}}}]},
        {"role": "tool", "tool_name": "f", "content": "{\"ok\": true}"},
    ]
    o = to_openai(msgs)
    assert o[0]["tool_calls"][0] == {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{\"x\": 1}"}}
    assert o[1] == {"role": "tool", "tool_call_id": "c1", "content": "{\"ok\": true}"}


async def test_groq_accumulates_tool_call_fragments():
    from types import SimpleNamespace as N

    def chunk(index, id=None, name=None, args=None):
        fn = N(name=name, arguments=args)
        return N(choices=[N(delta=N(content=None, tool_calls=[N(index=index, id=id, function=fn)]))])

    chunks = [chunk(0, "c0", "intent__HassTurnOn", '{"na'), chunk(0, None, None, 'me": "Luce"}'),
              chunk(1, "c1", "llm__GetDateTime", "")]

    class Stream:
        def __aiter__(self):
            async def gen():
                for c in chunks:
                    yield c
            return gen()

        async def close(self):
            pass

    llm = GroqLlm.__new__(GroqLlm)
    llm.client = N(chat=N(completions=N(create=lambda **kw: asyncio.sleep(0, Stream()))))
    out = [d async for d in llm.stream([], [{"type": "function"}])]
    assert out == [ToolCall("c0", "intent__HassTurnOn", {"name": "Luce"}), ToolCall("c1", "llm__GetDateTime", {})]


# -- safety -------------------------------------------------------------------------------


def _index(*entities):
    reg = ToolRegistry(None)
    reg.index_entities([{"entity_id": eid, "attributes": {"friendly_name": name, **({"device_class": dc} if dc else {})}}
                        for eid, name, dc in entities])
    return reg


HOME = _index(
    ("lock.front_door", "Front Door", None),
    ("lock.nuki", "Nuki", None),
    ("cover.garage", "Box auto", "garage"),
    ("cover.tapparella", "Tapparella sala", "shutter"),
    ("switch.relay_ingresso", "Apertura cancello", None),
    ("light.kitchen", "Luce cucina", None),
    ("light.porta", "Luce porta ingresso", None),
    ("script.apri_garage", "Apri garage", None),
    ("scene.esco_di_casa", "Esco e chiudo la porta", None),
    ("script.buongiorno", "Buongiorno", None),
)


def q(name, args, lang="it"):
    r = risk_of(ToolCall("1", name, args), HOME.lookup)
    return confirmation_question(r, lang) if r else None


def test_risk_policy_known_entities():
    assert q("intent__HassTurnOff", {"name": "Front Door"}) == "Vuoi davvero sbloccare Front Door?"
    assert "chiudere a chiave" in q("intent__HassTurnOn", {"name": "Front Door"})  # HA: TurnOn on a lock = lock
    assert q("intent__HassTurnOff", {"name": "Nuki"}) == "Vuoi davvero sbloccare Nuki?"  # no keyword, still a lock
    assert q("intent__HassTurnOn", {"name": "Box auto", "domain": ["cover"]}) == "Vuoi davvero aprire Box auto?"
    assert q("intent__HassSetPosition", {"name": "Box auto", "position": 100}) is not None
    assert q("intent__HassTurnOn", {"name": "Apertura cancello"}) is not None  # relay switch named like a gate
    assert q("intent__HassTurnOff", {"name": "Luce cucina"}) is None
    assert q("intent__HassTurnOn", {"name": "Luce porta ingresso"}) is None  # a light, even with "porta"
    assert q("intent__HassTurnOn", {"name": "Tapparella sala"}) is None  # shutters are not doors
    assert q("light__HassLightSet", {"name": "Porta luce", "brightness": 10}) is None


def test_risk_policy_scripts_and_scenes():
    """Scripts and scenes can drive anything: never safe by domain, judged by name."""
    assert q("intent__HassTurnOn", {"name": "Apri garage"}) == "Vuoi davvero avviare Apri garage?"
    assert q("intent__HassTurnOn", {"name": "Apri garage", "domain": ["script"]}) == "Vuoi davvero avviare Apri garage?"
    assert q("intent__HassTurnOn", {"name": "Esco e chiudo la porta", "domain": ["scene"]}, "en") == \
        "Do you really want to activate Esco e chiudo la porta?"
    assert q("intent__HassTurnOn", {"name": "Buongiorno", "domain": ["script"]}) is None  # known, harmless name
    assert q("intent__HassTurnOn", {"name": "Sconosciuto", "domain": ["script"]}) is not None  # unknown: confirm


def test_risk_policy_fails_closed():
    # Tool names without the "intent__" prefix (single-API MCP servers).
    assert q("HassTurnOff", {"name": "Front Door"}) == "Vuoi davvero sbloccare Front Door?"
    assert q("assist__HassTurnOff", {"name": "front door"}) is not None
    # Unknown names and area-wide commands: confirm, naming every possible effect.
    assert q("intent__HassTurnOff", {"name": "Porta di casa"}) == "Vuoi davvero spegnere, chiudere o sbloccare Porta di casa?"
    assert q("intent__HassTurnOn", {"area": "Esterno", "domain": ["cover"]}) is not None
    assert q("intent__HassTurnOn", {"area": "Cucina"}) is not None
    # Restricted to harmless domains: HA cannot match a lock.
    assert q("intent__HassTurnOff", {"area": "Cucina", "domain": ["light"]}) is None
    assert q("intent__HassTurnOff", {"name": "Front Door", "domain": ["lock"]}, "en") == "Do you really want to unlock Front Door?"


@pytest.mark.parametrize("command", ["spegni l'antifurto", "disinserisci l'allarme", "apri il portoncino", "apri il box",
                                     "apri la serratura", "turn off the alarm", "ouvre le portail", "öffne das Garagentor"])
def test_google_commands_with_risky_targets_need_confirmation(command):
    assert risk_of(ToolCall("1", "google_assistant", {"command": command})) is not None


def test_google_harmless_command():
    assert risk_of(ToolCall("1", "google_assistant", {"command": "accendi la tv del salotto"})) is None


def test_combined_question():
    a = risk_of(ToolCall("1", "HassTurnOff", {"name": "Front Door"}), HOME.lookup)
    b = risk_of(ToolCall("2", "HassTurnOn", {"name": "Box auto", "domain": ["cover"]}), HOME.lookup)
    assert confirmation_question([a, b], "it") == "Vuoi davvero sbloccare Front Door e aprire Box auto?"


@pytest.mark.parametrize("text,expected", [
    ("Sì, confermo.", True), ("si vai", True), ("Ok procedi", True), ("Yes, do it", True), ("sì grazie", True),
    ("va bene", True), ("certo", True),
    ("No, lascia stare.", False), ("annulla", False), ("sì... anzi no", False), ("sì ma non aprire", False),
    ("yes but not the front door", False), ("vai piano, non ancora", False), ("certo che no", False), ("aspetta", False),
    ("ok, e spegni la tv", None), ("si apre?", None), ("che ore sono?", None), ("accendi la luce", None),
])
def test_yes_no(text, expected):
    assert yes_no(text) is expected


# -- tool loop through the WebSocket --------------------------------------------------------

UTTERANCES[12000] = "spegni la luce della cucina"
UTTERANCES[12800] = "sblocca la porta di casa"
UTTERANCES[13600] = "sì, confermo"
UTTERANCES[14400] = "no, lascia stare"
UTTERANCES[15200] = "sblocca la porta e apri il box"
UTTERANCES[16000] = "accendi la luce e poi rompiti"


class FakeHome:
    """Stands in for Home Assistant behind a real ToolRegistry."""

    url = "fake"

    def __init__(self, delay=0.0):
        self.calls = []
        self.delay = delay
        self.services = set()

    async def list_tools(self):
        return [{"name": "intent__HassTurnOff", "description": "off", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}}]

    async def load_services(self):
        pass

    async def home_prompt(self):
        return "- names: Luce cucina\n  domain: light\n- names: Porta di casa\n  domain: lock\n"

    async def states(self):
        return [{"entity_id": "light.cucina", "attributes": {"friendly_name": "Luce cucina"}},
                {"entity_id": "lock.casa", "attributes": {"friendly_name": "Porta di casa"}}]

    async def call_tool(self, name, args):
        await asyncio.sleep(self.delay)
        self.calls.append((name, args))
        return {"ok": True, "result": '{"response_type": "action_done"}'}


class ToolLlm:
    """Round 1: a tool call chosen from the user text. Round 2: a spoken confirmation."""

    name = "fake"
    last_fallback = None
    using_remote = False

    def __init__(self):
        self.calls = []
        self.tools = []

    def set_tools(self, tools):
        self.tools = tools

    async def prefill(self, messages):
        pass

    fail_next = False

    async def stream(self, messages):
        self.calls.append(messages)
        await asyncio.sleep(0.01)
        if messages[-1]["role"] == "tool" and self.fail_next:
            self.fail_next = False
            raise RuntimeError("ollama crashed")
        if messages[-1]["role"] == "tool":
            yield "Fatto, luce spenta."
            return
        text = messages[-1]["content"]
        if "rompiti" in text and messages[-1]["role"] == "user":
            yield ToolCall("c9", "intent__HassTurnOff", {"name": "Luce cucina"})
            self.fail_next = True
            return
        if "porta e apri il box" in text:
            yield ToolCall("c3", "intent__HassTurnOff", {"name": "Porta di casa"})
            yield ToolCall("c4", "intent__HassTurnOff", {"name": "Box", "domain": ["cover"]})
            return
        if "luce" in text:
            yield ToolCall("c1", "intent__HassTurnOff", {"name": "Luce cucina"})
        elif "porta" in text:
            yield ToolCall("c2", "intent__HassTurnOff", {"name": "Porta di casa"})
        else:
            yield "Ok."


@pytest.fixture
def home(monkeypatch):
    sessions = []
    fake_home = FakeHome()
    llm = ToolLlm()
    svc = Services(stt=FakeStt(), llm=llm, tts=FakeTts(), langid=LangId(), tools=ToolRegistry(fake_home))
    monkeypatch.setattr(cfg, "warmup", False)
    monkeypatch.setattr(cfg, "local_hosts", [*cfg.local_hosts, "testserver"])  # TestClient Host
    monkeypatch.setattr(main, "build_services", lambda: svc)
    orig = Session.__init__

    def init(self, ws, s):
        orig(self, ws, s)
        sessions.append(self)

    monkeypatch.setattr(Session, "__init__", init)
    with TestClient(main.app) as c:
        c.sessions, c.llm, c.home = sessions, llm, fake_home
        yield c


def test_tool_runs_once_and_reply_is_spoken(home):
    with home.websocket_connect("/ws") as ws:
        assert recv(ws)["providers"]["home_tools"] == 1
        ws.send_bytes(utt(1, 12000))
        msgs = recv_until(ws, lambda m: m["type"] == "turn_end")
        assert home.home.calls == [("intent__HassTurnOff", {"name": "Luce cucina"})]
        assert next(m for m in msgs if m["type"] == "tool")["calls"][0]["name"] == "intent__HassTurnOff"
        assert [m["text"] for m in msgs if m["type"] == "sentence"] == ["Fatto, luce spenta."]
        # The second LLM round saw the tool result; the system prompt carries the device index.
        assert home.llm.calls[1][-1]["role"] == "tool"
        assert "Luce cucina" in home.llm.calls[0][0]["content"]
        h = home.sessions[0].history
        assert [m["role"] for m in h] == ["user", "assistant", "tool", "assistant"]
        assert h[1]["tool_calls"][0]["name"] == "intent__HassTurnOff"


def test_risky_action_needs_spoken_yes(home):
    with home.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_bytes(utt(1, 12800))
        msgs = recv_until(ws, lambda m: m["type"] == "turn_end")
        assert home.home.calls == []
        assert next(m for m in msgs if m["type"] == "confirm")["action"] == "Vuoi davvero sbloccare Porta di casa?"
        assert [m["text"] for m in msgs if m["type"] == "sentence"] == ["Vuoi davvero sbloccare Porta di casa?"]
        n_llm = len(home.llm.calls)
        ws.send_bytes(utt(2, 13600, msgs[0]["turn"], 0))
        msgs = recv_until(ws, lambda m: m["type"] == "turn_end")
        assert home.home.calls == [("intent__HassTurnOff", {"name": "Porta di casa"})]
        assert [m["text"] for m in msgs if m["type"] == "sentence"] == ["Fatto."]
        assert len(home.llm.calls) == n_llm  # decided in code, not by the LLM


def test_risky_action_cancelled_or_expired(home, monkeypatch):
    with home.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_bytes(utt(1, 12800))
        msgs = recv_until(ws, lambda m: m["type"] == "turn_end")
        ws.send_bytes(utt(2, 14400, msgs[0]["turn"], 0))
        msgs = recv_until(ws, lambda m: m["type"] == "turn_end")
        assert [m["text"] for m in msgs if m["type"] == "sentence"] == ["Va bene, annullato."]
        # Expired confirmation: "sì" is just a new request, nothing runs.
        monkeypatch.setattr(cfg, "confirm_ttl_s", 0.0)
        ws.send_bytes(utt(3, 12800, msgs[0]["turn"], 0))
        msgs = recv_until(ws, lambda m: m["type"] == "turn_end")
        ws.send_bytes(utt(4, 13600, msgs[0]["turn"], 0))
        recv_until(ws, lambda m: m["type"] == "turn_end")
        assert home.home.calls == []


def test_barge_in_during_slow_tool_does_not_rerun_or_merge(home, monkeypatch):
    home.home.delay = 0.3
    monkeypatch.setattr(cfg, "continuation_s", 60.0)  # would normally merge
    with home.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_bytes(utt(1, 12000))
        msgs = recv_until(ws, lambda m: m["type"] == "tool")
        ws.send_bytes(utt(2, 9600, msgs[0]["turn"], -1, playing=True))
        msgs = recv_until(ws, lambda m: m["type"] == "transcript")
        assert msgs[-1]["merged"] is False
        recv_until(ws, lambda m: m["type"] == "turn_end" and m["turn"] == msgs[-1]["turn"])
    import time
    time.sleep(0.4)
    assert home.home.calls == [("intent__HassTurnOff", {"name": "Luce cucina"})]  # exactly once
    h = home.sessions[0].history
    assert h[0]["content"] == UTTERANCES[12000]
    assert [m["role"] for m in h][-2:] == ["user", "assistant"]


def test_no_home_backend_means_no_tools():
    reg = ToolRegistry(None)
    asyncio.run(reg.start())
    assert reg.specs == [] and not reg.enabled


def test_confirm_buttons(home):
    with home.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_bytes(utt(1, 12800))
        recv_until(ws, lambda m: m["type"] == "turn_end")
        ws.send_text(json.dumps({"type": "confirm_answer", "yes": True}))
        msgs = recv_until(ws, lambda m: m["type"] == "turn_end")
        assert [m["text"] for m in msgs if m["type"] == "sentence"] == ["Fatto."]
        assert home.home.calls == [("intent__HassTurnOff", {"name": "Porta di casa"})]


def test_several_risky_actions_one_question(home):
    with home.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_bytes(utt(1, 15200))
        msgs = recv_until(ws, lambda m: m["type"] == "turn_end")
        confirms = [m for m in msgs if m["type"] == "confirm"]
        assert len(confirms) == 1
        assert confirms[0]["action"] == "Vuoi davvero sbloccare Porta di casa e chiudere Box?"
        ws.send_bytes(utt(2, 13600, msgs[0]["turn"], 0))
        recv_until(ws, lambda m: m["type"] == "turn_end")
        assert [c[1]["name"] for c in home.home.calls] == ["Porta di casa", "Box"]


def test_yes_before_question_was_spoken_does_nothing(home):
    with home.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_bytes(utt(1, 12800))
        ws.send_bytes(utt(2, 13600))  # "sì" right away, before the question audio
        recv_until(ws, lambda m: m["type"] == "turn_end" and m["turn"] >= 2)
        import time
        time.sleep(0.3)
    assert home.home.calls == []


def test_llm_failure_after_action_still_reports_outcome(home):
    with home.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_bytes(utt(1, 16000))
        msgs = recv_until(ws, lambda m: m["type"] == "turn_end")
        assert home.home.calls == [("intent__HassTurnOff", {"name": "Luce cucina"})]
        assert [m["text"] for m in msgs if m["type"] == "sentence"] == ["Fatto."]
        h = home.sessions[0].history
        assert [m["role"] for m in h] == ["user", "assistant", "tool", "assistant"]


def test_empty_llm_reply_is_retried(home):
    real = home.llm.stream
    state = {"n": 0}

    async def flaky(messages):
        state["n"] += 1
        if state["n"] == 1:
            return  # Ollama dropped the tool call: nothing at all
            yield
        async for d in real(messages):
            yield d

    home.llm.stream = flaky
    with home.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_bytes(utt(1, 12000))
        msgs = recv_until(ws, lambda m: m["type"] == "turn_end")
        assert home.home.calls == [("intent__HassTurnOff", {"name": "Luce cucina"})]
        assert msgs[-1]["metrics"]["llm_empty_retries"] == 1


class FlakyHome(FakeHome):
    """Unreachable for the first ``down`` attempts (VPN not up yet)."""

    def __init__(self, down=1):
        super().__init__()
        self.down = down

    async def list_tools(self):
        if self.down:
            self.down -= 1
            raise httpx.ConnectError("no route to host")
        return await super().list_tools()


async def test_registry_reports_unreachable_home_and_recovers(monkeypatch):
    monkeypatch.setattr(cfg, "ha_retry_s", 0.01)
    svc = Services(stt=FakeStt(), llm=ToolLlm(), tts=FakeTts(), langid=LangId(), tools=ToolRegistry(FlakyHome(down=2)))
    assert not await main.start_tools(svc)
    assert not svc.tools.enabled and svc.tools.context == ""
    await asyncio.wait_for(main.retry_tools(svc), 1)
    assert svc.tools.enabled and "Luce cucina" in svc.tools.context
    assert svc.llm.tools == svc.tools.specs


class GoogleHome(FakeHome):
    """Home Assistant with the Google Assistant SDK integration."""

    def __init__(self):
        super().__init__()
        self.services = {"google_assistant_sdk.send_text_command", "notify.google_assistant_sdk"}
        self.sent = []

    async def call_service(self, domain, service, data):
        self.sent.append((domain, service, data))
        return {"ok": True}


async def test_route_google_exposes_only_google_tools(monkeypatch):
    monkeypatch.setattr(cfg, "home_route", "google")
    svc = Services(stt=FakeStt(), llm=ToolLlm(), tts=FakeTts(), langid=LangId(), tools=ToolRegistry(GoogleHome()))
    assert await main.start_tools(svc)
    names = [s["function"]["name"] for s in svc.tools.specs]
    assert names == ["google_assistant", "announce"] and svc.llm.tools == svc.tools.specs
    assert "AWAY from home" in svc.tools.context and "Luce cucina" in svc.tools.context
    res = await svc.tools.execute(ToolCall("1", "google_assistant", {"command": "accendi la luce della cucina"}))
    assert res["ok"] and svc.tools.ha.sent[0][:2] == ("google_assistant_sdk", "send_text_command")


async def test_route_auto_switches_with_probe(monkeypatch):
    monkeypatch.setattr(cfg, "home_route", "auto")
    monkeypatch.setattr(cfg, "home_probe", "192.168.1.50:8009")
    monkeypatch.setattr(cfg, "home_probe_ttl_s", 0)
    at_home = True

    async def fake_probe(target, timeout=0.8):
        return at_home

    monkeypatch.setattr("server.tools.probe", fake_probe)
    reg = ToolRegistry(GoogleHome())
    await reg.start()
    assert not await reg.update_route() and reg.mode == "home"
    assert [s["function"]["name"] for s in reg.specs] == ["intent__HassTurnOff", "google_assistant", "announce"]
    at_home = False
    assert await reg.update_route() and reg.mode == "away"
    assert [s["function"]["name"] for s in reg.specs] == ["google_assistant", "announce"]
    at_home = True
    assert await reg.update_route() and reg.mode == "home" and "AWAY" not in reg.context


async def test_route_without_google_integration_stays_local(monkeypatch):
    monkeypatch.setattr(cfg, "home_route", "google")
    reg = ToolRegistry(FakeHome())
    await reg.start()
    assert not await reg.update_route() and reg.mode == "home"


def test_google_command_naming_a_risky_device_needs_confirmation(monkeypatch):
    monkeypatch.setattr(cfg, "confirm_risky", True)
    reg = ToolRegistry()
    reg.index_entities([{"entity_id": "lock.nuki", "attributes": {"friendly_name": "Nuki"}},
                        {"entity_id": "light.cucina", "attributes": {"friendly_name": "Luce cucina"}}])
    assert reg.risk(ToolCall("1", "google_assistant", {"command": "sblocca il Nuki"}))
    assert reg.risk(ToolCall("1", "google_assistant", {"command": "apri il cancello"}))
    assert reg.risk(ToolCall("1", "google_assistant", {"command": "accendi la luce cucina"})) is None
