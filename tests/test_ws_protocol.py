"""WebSocket protocol and barge-in semantics, with fake STT/LLM/TTS providers."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from server import main
from server.config import cfg
from server.langid import LangId
from server.session import AUDIO_HEADER, UTT_HEADER, Services, Session
from server.stt import Transcript

# The fake STT maps utterance length (in samples) to a transcript.
UTTERANCES = {
    8000: "Ciao, mi racconti qualcosa di bello sulla Toscana?",
    8800: "Sottotitoli creati dalla comunità Amara.org",
    9600: "aspetta un attimo per favore",
    10400: "e poi cosa mi consigli di mangiare",
}
REPLY = ["Certo, la Toscana è bellissima e piena di storia. ", "Ti consiglio Siena e la Val d'Orcia. ", "Poi anche Lucca e Pisa sono splendide. "]


class FakeStt:
    name = "fake"
    last_fallback = None

    async def warmup(self):
        pass

    async def transcribe(self, pcm):
        await asyncio.sleep(0.01)
        return Transcript(UTTERANCES[len(pcm)], provider="fake")


class FakeLlm:
    name = "fake"
    last_fallback = None
    using_remote = False

    def __init__(self, delay=0.05):
        self.delay = delay
        self.calls = []
        # Set by a test to hold the first reply after its first sentence, until the turn is interrupted. Sleeps shorter
        # than the clock resolution (15.6 ms on Windows) return at once, so delays alone cannot keep a reply going.
        self.hold: asyncio.Event | None = None

    async def warmup(self):
        pass

    def set_tools(self, tools):
        self.tools = tools

    async def prefill(self, messages):
        await asyncio.sleep(0.01)

    async def stream(self, messages):
        self.calls.append(messages)
        for i, sentence in enumerate(REPLY):
            if i and self.hold is not None and len(self.calls) == 1:
                await asyncio.wait_for(self.hold.wait(), 5)
            for word in sentence.split(" "):
                await asyncio.sleep(self.delay / 5)
                yield word + " "


class FakeTts:
    def supports(self, lang):
        return True

    async def preload(self, langs):
        pass

    async def stream(self, lang, text):
        for _ in range(3):
            await asyncio.sleep(0.02)
            yield b"\x01\x00" * 480


@pytest.fixture
def client(monkeypatch):
    sessions = []
    llm = FakeLlm()
    svc = Services(stt=FakeStt(), llm=llm, tts=FakeTts(), langid=LangId())
    monkeypatch.setattr(cfg, "warmup", False)
    monkeypatch.setattr(cfg, "local_hosts", [*cfg.local_hosts, "testserver"])  # TestClient Host
    monkeypatch.setattr(main, "build_services", lambda: svc)
    orig_init = Session.__init__

    def init(self, ws, s):
        orig_init(self, ws, s)
        sessions.append(self)

    monkeypatch.setattr(Session, "__init__", init)
    with TestClient(main.app) as c:
        c.sessions = sessions
        c.llm = llm
        yield c


def utt(utt_id, n_samples, heard_turn=-1, heard_seq=-1, playing=False):
    return UTT_HEADER.pack(utt_id, heard_turn, heard_seq, 1 if playing else 0) + b"\x00\x00" * n_samples


def recv(ws):
    m = ws.receive()
    if m.get("bytes") is not None:
        turn, seq = AUDIO_HEADER.unpack_from(m["bytes"])
        return {"type": "audio", "turn": turn, "seq": seq}
    return json.loads(m["text"])


def recv_until(ws, pred, limit=500):
    seen = []
    for _ in range(limit):
        m = recv(ws)
        seen.append(m)
        if pred(m):
            return seen
    raise AssertionError(f"condition not met; last={seen[-5:]}")


def test_full_turn(client):
    with client.websocket_connect("/ws") as ws:
        assert recv(ws)["type"] == "ready"
        ws.send_bytes(utt(1, 8000))
        msgs = recv_until(ws, lambda m: m["type"] == "turn_end")
        types = [m["type"] for m in msgs]
        assert types[:2] == ["turn_start", "transcript"]
        assert msgs[1]["lang"] == "it" and msgs[1]["merged"] is False
        sentences = [m for m in msgs if m["type"] == "sentence"]
        assert len(sentences) == 3
        audio = [m for m in msgs if m["type"] == "audio"]
        assert {m["seq"] for m in audio} == {0, 1, 2}
        assert all(m["turn"] == msgs[0]["turn"] for m in audio)
        assert "first_audio_ms" in msgs[-1]["metrics"]
        s = client.sessions[0]
        assert [m["role"] for m in s.history] == ["user", "assistant"]
        assert s.history[1]["content"].startswith("Certo, la Toscana")


def test_hallucination_skip_does_not_cancel_reply(client):
    with client.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_bytes(utt(1, 8000))
        msgs = recv_until(ws, lambda m: m["type"] == "audio")
        turn = msgs[0]["turn"]
        ws.send_bytes(utt(2, 8800, turn, 0, playing=True))
        msgs = recv_until(ws, lambda m: m["type"] == "turn_end")
        skip = [m for m in msgs if m["type"] == "skip"]
        assert skip and skip[0]["utt_id"] == 2 and skip[0]["reason"] == "hallucination"
        assert msgs[-1]["turn"] == turn
        assert not [m for m in msgs if m["type"] == "turn_start"]
        assert len(client.sessions[0].history) == 2


def test_echo_skip(client):
    UTTERANCES[11200] = "la toscana è bellissima e piena di storia"
    with client.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_bytes(utt(1, 8000))
        msgs = recv_until(ws, lambda m: m["type"] == "audio" and m["seq"] == 1)
        turn = msgs[0]["turn"]
        ws.send_bytes(utt(2, 11200, turn, 1, playing=True))
        msgs = recv_until(ws, lambda m: m["type"] == "skip")
        assert msgs[-1]["reason"] == "echo"


def test_foreign_language_while_playing_is_echo(client):
    """The bot's Italian voice coming back through the phone speaker as English words."""
    UTTERANCES[20800] = "And look at it."
    with client.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_bytes(utt(1, 8000))
        msgs = recv_until(ws, lambda m: m["type"] == "audio" and m["seq"] == 1)
        ws.send_bytes(utt(2, 20800, msgs[0]["turn"], 1, playing=True))
        msgs = recv_until(ws, lambda m: m["type"] == "skip")
        assert msgs[-1]["reason"] == "echo"


def test_barge_in_truncates_history_to_heard(client, monkeypatch):
    monkeypatch.setattr(cfg, "continuation_s", -60.0)  # disable: fake utterances "start" in the past
    with client.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_bytes(utt(1, 8000))
        msgs = recv_until(ws, lambda m: m["type"] == "audio" and m["seq"] == 1)
        turn = msgs[0]["turn"]
        # User heard sentence 0 only, then interrupted.
        ws.send_bytes(utt(2, 10400, turn, 0, playing=True))
        msgs = recv_until(ws, lambda m: m["type"] == "turn_start")
        new_turn = msgs[-1]["turn"]
        assert new_turn == turn + 1
        msgs = recv_until(ws, lambda m: m["type"] == "turn_end" and m["turn"] == new_turn)
        # No stale audio from the interrupted turn after the new turn started.
        assert all(m["turn"] == new_turn for m in msgs if m["type"] == "audio")
        h = client.sessions[0].history
        assert [m["role"] for m in h] == ["user", "assistant", "user", "assistant"]
        assert h[1]["content"] == "Certo, la Toscana è bellissima e piena di storia.…"
        assert h[2]["content"] == UTTERANCES[10400]
        # The LLM saw the truncated reply.
        assert client.llm.calls[-1][2]["content"].endswith("…")


def test_nothing_heard_merges_utterances(client):
    with client.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_bytes(utt(1, 9600))
        recv_until(ws, lambda m: m["type"] == "transcript")
        ws.send_bytes(utt(2, 10400))  # before any audio was heard
        msgs = recv_until(ws, lambda m: m["type"] == "transcript")
        assert msgs[-1]["merged"] is True
        assert msgs[-1]["text"] == f"{UTTERANCES[9600]} {UTTERANCES[10400]}"
        recv_until(ws, lambda m: m["type"] == "turn_end" and m["turn"] == msgs[-1]["turn"])
        h = client.sessions[0].history
        assert [m["role"] for m in h] == ["user", "assistant"]


def test_resuming_right_after_reply_starts_is_a_continuation(client, monkeypatch):
    monkeypatch.setattr(cfg, "continuation_s", 5.0)
    with client.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_bytes(utt(1, 9600))
        msgs = recv_until(ws, lambda m: m["type"] == "audio")
        turn = msgs[0]["turn"]
        # User heard the first bit of sentence 0 but was actually still talking.
        ws.send_bytes(utt(2, 10400, turn, 0, playing=True))
        msgs = recv_until(ws, lambda m: m["type"] == "transcript")
        assert msgs[-1]["merged"] is True
        recv_until(ws, lambda m: m["type"] == "turn_end" and m["turn"] == msgs[-1]["turn"])
        h = client.sessions[0].history
        assert [m["role"] for m in h] == ["user", "assistant"]
        assert h[0]["content"] == f"{UTTERANCES[9600]} {UTTERANCES[10400]}"


def test_speech_start_prefill_is_harmless(client):
    with client.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_text(json.dumps({"type": "speech_start"}))
        ws.send_bytes(utt(1, 8000))
        recv_until(ws, lambda m: m["type"] == "turn_end")


def test_fully_heard_reply_is_kept(client):
    with client.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_bytes(utt(1, 8000))
        msgs = recv_until(ws, lambda m: m["type"] == "turn_end")
        turn = msgs[0]["turn"]
        ws.send_bytes(utt(2, 10400, turn, 2, playing=False))
        recv_until(ws, lambda m: m["type"] == "turn_end")
        h = client.sessions[0].history
        assert h[1]["content"] == " ".join(s.strip() for s in REPLY)


def test_short_utterance_skipped(client):
    with client.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_bytes(utt(5, 100))
        m = recv(ws)
        assert m == {"type": "skip", "utt_id": 5, "reason": "short"}


def test_greeting_and_reset(client):
    with client.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_text(json.dumps({"type": "hello"}))
        msgs = recv_until(ws, lambda m: m["type"] == "turn_end")
        assert any(m["type"] == "sentence" for m in msgs)
        assert any(m["type"] == "audio" for m in msgs)
        assert client.sessions[0].history == []  # greeting is not part of the history
        ws.send_text(json.dumps({"type": "reset"}))
        recv_until(ws, lambda m: m["type"] == "reset_done")


def test_tap_interrupt_truncates_history(client):
    with client.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_bytes(utt(1, 8000))
        msgs = recv_until(ws, lambda m: m["type"] == "audio" and m["seq"] == 1)
        turn = msgs[0]["turn"]
        ws.send_text(json.dumps({"type": "interrupt", "heard_turn": turn, "heard_seq": 0}))
        ws.send_bytes(utt(2, 10400, turn, 0, playing=False))
        recv_until(ws, lambda m: m["type"] == "turn_end" and m["turn"] == turn + 1)
        h = client.sessions[0].history
        assert [m["role"] for m in h] == ["user", "assistant", "user", "assistant"]
        assert h[1]["content"] == "Certo, la Toscana è bellissima e piena di storia.…"


def test_failed_turn_does_not_erase_previous_exchange(client, monkeypatch):
    with client.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_bytes(utt(1, 8000))
        msgs = recv_until(ws, lambda m: m["type"] == "turn_end")
        turn_a = msgs[0]["turn"]

        async def boom(messages):
            raise RuntimeError("llm down")
            yield  # pragma: no cover

        monkeypatch.setattr(client.llm, "stream", boom)
        ws.send_bytes(utt(2, 9600, turn_a, 2, playing=False))
        recv_until(ws, lambda m: m["type"] == "turn_end" and m["turn"] == turn_a + 1)
        # Next utterance arrives while the client still thinks audio is playing.
        monkeypatch.undo()
        ws.send_bytes(utt(3, 10400, turn_a + 1, -1, playing=True))
        msgs = recv_until(ws, lambda m: m["type"] == "transcript")
        assert msgs[-1]["merged"] is False
        recv_until(ws, lambda m: m["type"] == "turn_end" and m["turn"] == turn_a + 2)
        h = client.sessions[0].history
        assert [m["role"] for m in h] == ["user", "assistant", "user", "assistant"]
        assert h[0]["content"] == UTTERANCES[8000]


def test_utterance_in_flight_during_reset_is_dropped(client, monkeypatch):
    stt = main.build_services().stt
    orig = stt.transcribe

    async def slow(pcm):
        await asyncio.sleep(0.2)
        return await orig(pcm)

    monkeypatch.setattr(stt, "transcribe", slow)
    with client.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_bytes(utt(1, 8000))
        ws.send_text(json.dumps({"type": "reset"}))
        msgs = recv_until(ws, lambda m: m["type"] == "skip")
        assert msgs[-1]["reason"] == "reset"
        assert client.sessions[0].history == []


def text_msg(text, **kw):
    return json.dumps({"type": "text", "text": text, **kw})


def test_typed_message_without_voice_is_text_only(client):
    with client.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_text(text_msg("  Ciao, mi racconti qualcosa\n di bello sulla Toscana?  "))
        msgs = recv_until(ws, lambda m: m["type"] == "turn_end")
        tr = next(m for m in msgs if m["type"] == "transcript")
        assert tr["text"] == UTTERANCES[8000]
        assert tr["stt"] == "typed" and tr["merged"] is False
        assert len([m for m in msgs if m["type"] == "sentence"]) == 3
        assert not [m for m in msgs if m["type"] in ("audio", "tts_fallback")]
        assert [m["role"] for m in client.sessions[0].history] == ["user", "assistant"]


def test_empty_typed_message_ignored(client):
    with client.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_text(text_msg(" ?! "))
        ws.send_text(text_msg("ciao"))
        msgs = recv_until(ws, lambda m: m["type"] == "transcript")
        assert msgs[-1]["text"] == "ciao"


def test_typed_message_in_voice_session_is_spoken(client):
    with client.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_text(json.dumps({"type": "hello", "greet": False}))
        ws.send_text(text_msg("ciao, come stai?"))
        msgs = recv_until(ws, lambda m: m["type"] == "turn_end")
        assert {m["seq"] for m in msgs if m["type"] == "audio"} == {0, 1, 2}
        ws.send_text(json.dumps({"type": "voice", "on": False}))
        ws.send_text(text_msg("e domani?"))
        msgs = recv_until(ws, lambda m: m["type"] == "turn_end")
        assert not [m for m in msgs if m["type"] == "audio"]


def test_typed_message_interrupts_without_merging(client):
    client.llm.hold = asyncio.Event()  # never set: the first reply stops after one sentence
    with client.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_text(text_msg("raccontami della Toscana"))
        msgs = recv_until(ws, lambda m: m["type"] == "sentence" and m["seq"] == 0)
        turn = msgs[0]["turn"]
        ws.send_text(text_msg("anzi, parlami di Roma", heard_turn=turn, heard_seq=0))
        msgs = recv_until(ws, lambda m: m["type"] == "transcript")
        assert msgs[-1]["merged"] is False and msgs[-1]["text"] == "anzi, parlami di Roma"
        recv_until(ws, lambda m: m["type"] == "turn_end" and m["turn"] == turn + 1)
        h = client.sessions[0].history
        assert [m["role"] for m in h] == ["user", "assistant", "user", "assistant"]
        assert h[1]["content"] == "Certo, la Toscana è bellissima e piena di storia.…"


CLIENT = "0123456789abcdef0123"


def test_reconnecting_client_resumes_its_conversation(client):
    with client.websocket_connect(f"/ws?client={CLIENT}") as ws:
        assert recv(ws)["resumed"] == 0
        ws.send_json({"type": "text", "text": "Ciao, mi racconti qualcosa sulla Toscana?"})
        recv_until(ws, lambda m: m["type"] == "turn_end")
    # Same client id (screen unlocked, network changed): the conversation goes on, no greeting.
    with client.websocket_connect(f"/ws?client={CLIENT}") as ws:
        assert recv(ws)["resumed"] == 1
        ws.send_json({"type": "hello"})
        ws.send_json({"type": "text", "text": "e poi cosa mi consigli di mangiare"})
        msgs = recv_until(ws, lambda m: m["type"] == "turn_end")
        assert not any(m["type"] == "sentence" and "Sono pronto" in m["text"] for m in msgs)
        history = client.sessions[-1].history
        assert history[0]["content"].startswith("Ciao, mi racconti") and len(history) == 4
    # Another client, or none, starts from scratch.
    with client.websocket_connect("/ws?client=ffffffffffffffffffff") as ws:
        assert recv(ws)["resumed"] == 0
    with client.websocket_connect("/ws") as ws:
        assert recv(ws)["resumed"] == 0


def test_saved_history_expires(client, monkeypatch):
    monkeypatch.setattr(cfg, "client_history_ttl_s", 0)
    with client.websocket_connect(f"/ws?client={CLIENT}x") as ws:
        recv(ws)
        ws.send_json({"type": "text", "text": "Ciao, mi racconti qualcosa sulla Toscana?"})
        recv_until(ws, lambda m: m["type"] == "turn_end")
    with client.websocket_connect(f"/ws?client={CLIENT}x") as ws:
        assert recv(ws)["resumed"] == 0
