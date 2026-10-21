"""One WebSocket conversation: turn-taking, barge-in, history and the STT->LLM->TTS pipeline.

Wire protocol (see README for the full table):
- client -> server binary: 16-byte header ``<IiiI`` = utt_id, heard_turn, heard_seq, flags
  (bit 0: bot audio was playing when the user started speaking), then Int16 LE PCM, 16 kHz mono.
- server -> client binary: 8-byte header ``<II`` = turn, seq, then Int16 LE PCM, 24 kHz mono.
- everything else is JSON text messages. Typed chat: ``{"type": "text", "text", "heard_turn",
  "heard_seq", "playing"}``; replies are spoken only while voice is on (``hello`` or mic audio turn
  it on, ``{"type": "voice", "on": false}`` turns it off).
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np
from fastapi import WebSocket, WebSocketDisconnect

from .chunker import Chunker
from .config import cfg
from .langid import BCP47, LangId
from .llm import LlmRouter
from .prompts import CANCELLED, DONE, FAILED, GREETINGS, PARTIAL, build_messages, set_home_context
from .stats import Stats
from .stt import SAMPLE_RATE, SttRouter
from .textclean import clean_for_tts, is_echo, is_hallucination
from .tools import PendingAction, ToolCall, ToolRegistry, confirmation_question, new_pending, yes_no
from .tts import SAMPLE_RATE as TTS_RATE
from .tts import PocketTts

log = logging.getLogger("voicebot.session")

UTT_HEADER = struct.Struct("<IiiI")
MAX_TEXT_CHARS = 2000  # typed chat message
MAX_UTT_BYTES = UTT_HEADER.size + 30 * SAMPLE_RATE * 2  # 30 s of 16 kHz PCM16
AUDIO_HEADER = struct.Struct("<II")
FLAG_PLAYING = 1


def _int(v, default: int) -> int:
    """Integer field sent by the client; garbage becomes ``default`` instead of an exception."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


@dataclass
class Services:
    stt: SttRouter
    llm: LlmRouter
    tts: PocketTts
    langid: LangId
    tools: ToolRegistry = field(default_factory=ToolRegistry)
    stats: Stats | None = None  # per-turn latency log for /stats (None: disabled)
    # Conversation kept per client after its WebSocket closes: the phone drops it on screen
    # lock or network change and reconnects with the same client id (web/app.js).
    histories: dict[str, SavedHistory] = field(default_factory=dict)


@dataclass
class SavedHistory:
    history: list[dict]
    lang: str
    saved_at: float


MAX_SAVED_HISTORIES = 20


@dataclass
class Turn:
    id: int
    utt_id: int
    user_text: str | None  # None for the greeting
    lang: str
    t0: float
    sentences: list[str] = field(default_factory=list)  # index == audio seq
    done: bool = False
    in_history: bool = True  # the user message is in history (False for greeting / failed turn)
    replied: bool = False  # the assistant message for this turn is in history
    task: asyncio.Task | None = None
    first_audio_at: float | None = None  # perf_counter when the first audio left the server
    actions: list[str] = field(default_factory=list)  # tool actions executed in this turn
    hist_start: int = 0  # index of this turn's user message in history
    tools_started: bool = False  # a tool was sent to the house (maybe still running)
    superseded: bool = False  # a newer turn replaced this one: it must not act any more
    question: str | None = None  # confirmation question asked: becomes the history reply
    speak: bool = True  # synthesize audio (False: text-only chat, no voice session running)
    metrics: dict[str, float] = field(default_factory=dict)

    def mark(self, key: str) -> None:
        if key not in self.metrics:
            self.metrics[key] = round((time.perf_counter() - self.t0) * 1000)
            if key == "first_audio_ms":
                self.first_audio_at = time.perf_counter()


class Session:
    def __init__(self, ws: WebSocket, svc: Services) -> None:
        self.ws = ws
        self.svc = svc
        self.history: list[dict] = []
        self.lang = cfg.default_lang
        self.turn_counter = 0
        self.current: Turn | None = None
        self.out: asyncio.Queue = asyncio.Queue()
        self.lock = asyncio.Lock()
        self.tasks: set[asyncio.Task] = set()
        self.greeted = False
        self.pending: PendingAction | None = None  # risky action waiting for a spoken "sì"
        self.prefilling = False
        self.epoch = 0  # bumped on reset: utterances transcribed across a reset are dropped
        self.voice = False  # the client has a voice session running (mic + player)
        self.confirm_ttl_s: float | None = None  # time to answer "are you sure?" (None: CONFIRM_TTL_S)
        cid = str(ws.query_params.get("client", "")) if ws is not None else ""
        self.client_id = cid if 16 <= len(cid) <= 64 and cid.replace("-", "").isalnum() else ""

    # -- transport --------------------------------------------------------------------

    def send(self, msg: dict) -> None:
        self.out.put_nowait(msg)

    def send_audio(self, turn: Turn, seq: int, pcm16: bytes) -> bool:
        if self.current is not turn:
            return False  # stale turn: never send its audio
        self.out.put_nowait(AUDIO_HEADER.pack(turn.id, seq) + pcm16)
        return True

    async def _writer(self) -> None:
        while (item := await self.out.get()) is not None:
            try:
                if isinstance(item, bytes):
                    await self.ws.send_bytes(item)
                else:
                    await self.ws.send_text(json.dumps(item, ensure_ascii=False))
            except Exception:  # noqa: BLE001 - socket closed
                return

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(_log_task_error)
        return task

    def _resume_history(self) -> int:
        """Continue this client's conversation if it reconnected recently. Returns its turns."""
        saved = self.svc.histories.pop(self.client_id, None) if self.client_id else None
        if saved is None or time.monotonic() - saved.saved_at >= cfg.client_history_ttl_s:
            return 0
        self.history, self.lang = saved.history, saved.lang
        self.greeted = True  # mid-conversation: no greeting
        return sum(m["role"] == "user" for m in self.history)

    def _save_history(self) -> None:
        if not self.client_id or not self.history:
            return
        # A pending confirmation is never carried over: after a reconnect the bot asks again.
        self.svc.histories[self.client_id] = SavedHistory(list(self.history), self.lang, time.monotonic())
        now = time.monotonic()
        stale = [k for k, v in self.svc.histories.items() if now - v.saved_at >= cfg.client_history_ttl_s]
        for k in stale:
            del self.svc.histories[k]
        while len(self.svc.histories) > MAX_SAVED_HISTORIES:
            del self.svc.histories[min(self.svc.histories, key=lambda k: self.svc.histories[k].saved_at)]

    async def run(self) -> None:
        writer = asyncio.create_task(self._writer())
        resumed = self._resume_history()
        self.send({
            "type": "ready",
            "tts_rate": TTS_RATE,
            "stt_rate": SAMPLE_RATE,
            "lang": self.lang,
            "vad": {"redemption_ms": cfg.vad_redemption_ms},
            "resumed": resumed,
            "providers": {"stt": self.svc.stt.name, "llm": self.svc.llm.name,
                          "home_tools": len(self.svc.tools.specs),
                          "llm_model": cfg.groq_llm_model if self.svc.llm.name == "groq" else cfg.llm_model,
                          "tts": "pocket"},
        })
        # A client just connected (e.g. the phone app opened): page the model back into memory now,
        # not on the first question. After idle time macOS may have swapped it out (~15 s stall).
        self._spawn(self.prefill())
        try:
            while True:
                msg = await self.ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if (data := msg.get("bytes")) is not None:
                    if len(data) > MAX_UTT_BYTES:
                        self.send({"type": "error", "stage": "protocol", "msg": "utterance too long"})
                        continue
                    self._spawn(self.handle_utterance(data))
                elif (text := msg.get("text")) is not None:
                    try:
                        m = json.loads(text)
                    except ValueError:
                        m = None
                    if not isinstance(m, dict):
                        self.send({"type": "error", "stage": "protocol", "msg": "invalid message"})
                        continue
                    self.handle_control(m)
        except WebSocketDisconnect:
            pass
        finally:
            if self.current and self.current.task:
                self.current.task.cancel()
            for t in list(self.tasks):
                t.cancel()
            self._save_history()
            self.out.put_nowait(None)
            await asyncio.gather(writer, return_exceptions=True)

    def handle_control(self, m: dict) -> None:
        kind = m.get("type")
        if kind == "hello":
            self.voice = True
            if not self.greeted and m.get("greet", True):
                self.greeted = True
                self._spawn(self.greet())
        elif kind == "voice":
            self.voice = bool(m.get("on"))
        elif kind == "text":
            self._spawn(self.handle_text(m))
        elif kind == "speech_start":
            busy = self.current is not None and not self.current.done
            if cfg.prefill_on_speech and not busy and not self.prefilling:
                self._spawn(self.prefill())
        elif kind == "interrupt":
            # User silenced the bot (tap): stop generating, keep only what was heard.
            cur = self.current
            if cur is not None and m.get("heard_turn") == cur.id:
                self._supersede(cur)
                cur.done = True
                self._truncate_to_heard(cur, _int(m.get("heard_seq"), -1))
        elif kind == "confirm_answer" and self.pending is not None and self.pending.asked:
            # Sì/No buttons: same path as answering by voice.
            self._spawn(self._answer_pending("sì" if m.get("yes") else "no"))
        elif kind == "reset":
            self.epoch += 1
            if self.current:
                self._supersede(self.current)
            self.pending = None
            self.current = None
            self.history.clear()
            self.lang = cfg.default_lang
            self.send({"type": "reset_done"})

    # -- turns --------------------------------------------------------------------------

    async def prefill(self) -> None:
        """User started talking: wake the GPU (it clocks down when idle, adding ~0.4s to the
        next LLM call) and cache the conversation prefix, so the reply starts sooner."""
        self.prefilling = True
        try:
            msgs = build_messages([*self.history, {"role": "user", "content": "…"}], self.lang,
                                  with_tools=self.svc.tools.enabled)
            stt_tick = getattr(self.svc.stt.primary, "tick", None)
            await asyncio.gather(self.svc.llm.prefill(msgs), *([stt_tick()] if stt_tick else []))
        except Exception as e:  # noqa: BLE001
            log.debug("prefill failed: %s", e)
        finally:
            self.prefilling = False

    async def greet(self) -> None:
        async with self.lock:
            turn = self._new_turn(utt_id=0, user_text=None, lang=self.lang)
        turn.task = asyncio.current_task()
        await self._speak_sentence(turn, GREETINGS.get(turn.lang, GREETINGS["en"]))
        turn.done = True
        self.send({"type": "turn_end", "turn": turn.id, "metrics": turn.metrics})

    async def _answer_pending(self, text: str) -> None:
        async with self.lock:
            turn = self._new_turn(utt_id=0, user_text=text, lang=self.lang)
            turn.task = self._spawn(self.run_turn(turn))

    async def handle_utterance(self, data: bytes) -> None:
        t0 = time.perf_counter()
        utt_id, heard_turn, heard_seq, flags = UTT_HEADER.unpack_from(data)
        pcm = np.frombuffer(data, dtype="<i2", offset=UTT_HEADER.size).astype(np.float32) / 32768.0
        playing = bool(flags & FLAG_PLAYING)
        epoch = self.epoch
        self.voice = True  # mic audio: a voice session is running

        async with self.lock:  # serialize decisions; STT is single-threaded anyway
            if len(pcm) < SAMPLE_RATE * cfg.min_utterance_ms / 1000:
                return self.send({"type": "skip", "utt_id": utt_id, "reason": "short"})
            try:
                tr = await self.svc.stt.transcribe(pcm)
            except Exception as e:
                log.exception("STT failed")
                self.send({"type": "error", "stage": "stt", "msg": str(e)})
                return self.send({"type": "skip", "utt_id": utt_id, "reason": "stt_error"})
            stt_ms = round((time.perf_counter() - t0) * 1000)
            if epoch != self.epoch:
                return self.send({"type": "skip", "utt_id": utt_id, "reason": "reset"})
            if self.svc.stt.last_fallback:
                self.send({"type": "error", "stage": "stt", "msg": self.svc.stt.last_fallback, "fallback": "parakeet"})

            text = tr.text.strip()
            reason = None
            if not any(ch.isalnum() for ch in text):
                reason = "empty"
            elif tr.no_speech:
                reason = "noise"
            elif is_hallucination(text):
                reason = "hallucination"
            elif playing and self.current and is_echo(text, self._heard_sentences(heard_turn, heard_seq)):
                reason = "echo"
            elif playing and self.svc.langid.foreign(text, self.lang):
                reason = "echo"  # the bot's own voice, mis-transcribed in another language
            if reason:
                log.info("skip utt=%d reason=%s text=%r", utt_id, reason, text)
                return self.send({"type": "skip", "utt_id": utt_id, "reason": reason, "text": text})

            lang = self.svc.langid.requested(text) or self.svc.langid.resolve(text, self.lang, tr.lang)
            self.lang = lang
            speech_start = t0 - len(pcm) / SAMPLE_RATE
            merged = self._interrupt_current(heard_turn, heard_seq, playing, speech_start)
            user_text = f"{merged} {text}" if merged else text
            turn = self._new_turn(utt_id, user_text, lang, t0)
            turn.metrics["stt_ms"] = stt_ms
            self.send({"type": "transcript", "turn": turn.id, "utt_id": utt_id, "text": user_text,
                       "lang": lang, "stt": tr.provider, "merged": bool(merged)})
            turn.task = self._spawn(self.run_turn(turn))

    async def handle_text(self, m: dict) -> None:
        """Typed chat message: same turn pipeline as speech, minus STT. The reply is spoken
        only while a voice session is running."""
        async with self.lock:
            self.run_text(str(m.get("text", "")), _int(m.get("heard_turn"), -1), _int(m.get("heard_seq"), -1),
                          bool(m.get("playing")))

    def run_text(self, text: str, heard_turn: int = -1, heard_seq: int = -1, playing: bool = False) -> Turn | None:
        """Start a turn for a typed message (chat, Siri Shortcut). The caller holds ``self.lock``."""
        text = " ".join(text.split())[:MAX_TEXT_CHARS]
        if not any(ch.isalnum() for ch in text):
            return None
        t0 = time.perf_counter()
        lang = self.svc.langid.requested(text) or self.svc.langid.resolve(text, self.lang)
        self.lang = lang
        # Typed text is never a continuation of the previous message: no merge.
        self._interrupt_current(heard_turn, heard_seq, playing, t0, merge=False)
        turn = self._new_turn(0, text, lang, t0)
        self.send({"type": "transcript", "turn": turn.id, "utt_id": 0, "text": text,
                   "lang": lang, "stt": "typed", "merged": False})
        turn.task = self._spawn(self.run_turn(turn))
        return turn

    def _supersede(self, turn: Turn) -> None:
        """Stop ``turn`` for good: it may not act or ask anything after this point."""
        turn.superseded = True
        if turn.task and not turn.done:
            turn.task.cancel()
        p = self.pending
        if p is not None and p.turn_id == turn.id and not p.asked:
            self.pending = None  # its question was never spoken: nothing to answer

    def _heard_sentences(self, heard_turn: int, heard_seq: int) -> list[str]:
        """Bot sentences the user could have heard: echo candidates."""
        cur = self.current
        if cur is None or heard_turn != cur.id or heard_seq < 0:
            return []
        return cur.sentences[max(0, heard_seq - 1): heard_seq + 1]

    def _new_turn(self, utt_id: int, user_text: str | None, lang: str, t0: float | None = None) -> Turn:
        if self.current is not None:
            self._supersede(self.current)
        self.turn_counter += 1
        turn = Turn(self.turn_counter, utt_id, user_text, lang, t0 or time.perf_counter(), speak=self.voice)
        if user_text is not None:
            self._trim_history()
            turn.hist_start = len(self.history)
            self.history.append({"role": "user", "content": user_text})
        self.current = turn
        self.send({"type": "turn_start", "turn": turn.id, "utt_id": utt_id, "lang": lang})
        return turn

    def _interrupt_current(self, heard_turn: int, heard_seq: int, playing: bool, speech_start: float,
                           merge: bool = True) -> str | None:
        """Stop the current reply and fix history to what the user actually heard.

        Returns the previous user text when the user was most likely still talking (nothing
        of the reply was heard, or they resumed right after it started), so the new
        utterance is merged with it (only when ``merge``).
        """
        cur = self.current
        if cur is None:
            return None
        self._supersede(cur)
        heard = heard_seq if heard_turn == cur.id else -1
        if cur.done and not playing:
            # Reply finished and the bot is silent: the user heard all of it (or chose to
            # skip it). Keep it as is.
            return None
        if cur.user_text is None or not cur.in_history:
            return None  # greeting, or a failed turn whose user message was already removed
        if cur.tools_started:
            # Something was (or is being) done in the house: never merge, it would run again.
            self._truncate_to_heard(cur, heard, fallback=DONE.get(cur.lang, DONE["en"]) if cur.actions else "")
            return None
        continuation = cur.first_audio_at is not None and speech_start < cur.first_audio_at + cfg.continuation_s
        if not merge or (heard >= 0 and not continuation):
            self._truncate_to_heard(cur, heard)
            return None
        # Nothing (meaningful) heard: drop this turn's messages and merge the user texts.
        del self.history[cur.hist_start:]
        cur.in_history = cur.replied = False
        return cur.user_text

    def _truncate_to_heard(self, cur: Turn, heard_seq: int, fallback: str = "") -> None:
        """Make ``cur``'s assistant message contain only the sentences the user heard."""
        if not cur.in_history:
            return
        heard_text = " ".join(cur.sentences[: heard_seq + 1]) or fallback
        content = heard_text + "…" if heard_text else "…"
        if cur.replied:
            self.history[-1]["content"] = content
        else:
            self.history.append({"role": "assistant", "content": content})
            cur.replied = True

    def _trim_history(self) -> None:
        starts = [i for i, m in enumerate(self.history) if m["role"] == "user"]
        if len(starts) >= cfg.history_max_turns:
            # Trim in blocks so the prompt prefix stays stable (KV cache reuse) on most turns.
            self.history = self.history[starts[-cfg.history_keep_turns]:]

    async def run_turn(self, turn: Turn) -> None:
        if self.pending is not None and await self._resolve_pending(turn):
            return self._finish_turn(turn)

        llm = self.svc.llm
        tools = self.svc.tools
        if await tools.update_route():  # this computer left or reached the home network
            llm.set_tools(tools.specs)
            set_home_context(tools.context)
        messages = build_messages(
            self.history, turn.lang, cfg.groq_history_turns if llm.using_remote else None, with_tools=tools.enabled
        )
        sentences: asyncio.Queue[str | None] = asyncio.Queue()

        fallbacks: list[str] = []

        async def producer() -> None:
            chunker = Chunker()
            msgs = messages
            failed = unfinished = False
            try:
                for rnd in range(cfg.tool_max_rounds + 1):
                    calls: list[ToolCall] = []
                    text: list[str] = []
                    t_round = time.perf_counter()
                    ttft_key = "llm_ttft_ms" if rnd == 0 else "llm_after_tools_ttft_ms"
                    for attempt in range(cfg.llm_empty_retries + 1):
                        async for delta in llm.stream(msgs):
                            if isinstance(delta, ToolCall):
                                turn.metrics.setdefault("llm_tool_call_ms", round((time.perf_counter() - t_round) * 1000))
                                calls.append(delta)
                                continue
                            turn.metrics.setdefault(ttft_key, round((time.perf_counter() - t_round) * 1000))
                            self.send({"type": "token", "turn": turn.id, "text": delta})
                            text.append(delta)
                            for s in chunker.feed(delta):
                                sentences.put_nowait(s)
                        if calls or "".join(text).strip():
                            break
                        # Ollama's Gemma 4 parser sometimes drops a tool call and returns nothing
                        # at all. Nothing was said yet, so retrying is invisible to the user.
                        log.warning("turn %d: empty LLM reply (round %d), retry %d", turn.id, rnd, attempt + 1)
                        turn.metrics["llm_empty_retries"] = turn.metrics.get("llm_empty_retries", 0) + 1
                    if llm.last_fallback:
                        fallbacks.append(llm.last_fallback)
                    if not calls:
                        break
                    if rnd == cfg.tool_max_rounds:
                        log.warning("turn %d: tool round limit reached, %d calls not run", turn.id, len(calls))
                        unfinished = True
                        break
                    for s in chunker.flush():  # speak any preamble while the tools run
                        sentences.put_nowait(s)
                    self.send({"type": "tool", "turn": turn.id,
                               "calls": [{"name": c.name, "args": c.args} for c in calls]})
                    results = await self._run_tools(turn, calls)
                    failed = failed or any(not r.get("ok", True) and r.get("status") != "needs_confirmation" for r in results)
                    if any(r.get("status") == "needs_confirmation" for r in results):
                        # Ask with a question built from the real actions, not the LLM's paraphrase.
                        if self.pending is not None and self.pending.turn_id == turn.id:
                            sentences.put_nowait(("confirm", self.pending.question))
                        return
                    if "".join(text).strip():
                        log.info("turn %d: text before tool calls: %r", turn.id, "".join(text))
                    call_msg = {"role": "assistant", "content": "".join(text),
                                "tool_calls": [{"id": c.id, "name": c.name, "args": c.args} for c in calls]}
                    tool_msgs = [{"role": "tool", "tool_call_id": c.id, "name": c.name,
                                  "content": json.dumps(r, ensure_ascii=False)} for c, r in zip(calls, results)]
                    msgs = [*msgs, call_msg, *tool_msgs]
                    if self.current is turn and turn.in_history:
                        # Keep the tool exchange in history (results shortened), so the model sees
                        # that actions happen through tools, not just by saying "fatto".
                        self.history.append(call_msg)
                        self.history.extend({**m, "content": m["content"][:300]} for m in tool_msgs)
                for s in chunker.flush():
                    sentences.put_nowait(s)
                if unfinished:
                    sentences.put_nowait(PARTIAL.get(turn.lang, PARTIAL["en"]))
                elif not turn.sentences and sentences.empty() and (turn.actions or failed):
                    # The model acted but said nothing: give a short spoken outcome.
                    words = FAILED if failed and not turn.actions else DONE
                    sentences.put_nowait(words.get(turn.lang, words["en"]))
            finally:
                sentences.put_nowait(None)

        async def consumer() -> None:
            while (s := await sentences.get()) is not None:
                if isinstance(s, tuple):  # ("confirm", question)
                    await self._speak_sentence(turn, s[1])
                    if self.pending is not None and self.pending.turn_id == turn.id and not turn.superseded:
                        self.pending.asked = True
                        turn.question = s[1]
                    continue
                await self._speak_sentence(turn, s)

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(producer())
                tg.create_task(consumer())
        except Exception as eg:  # noqa: BLE001 - ExceptionGroup; CancelledError passes through
            err = eg.exceptions[0] if isinstance(eg, BaseExceptionGroup) else eg
            log.error("turn %d failed: %r", turn.id, err)
            self.send({"type": "error", "stage": "llm", "turn": turn.id, "msg": str(err)})
            if turn.tools_started and not turn.sentences:
                # Something ran in the house: never leave the user without an outcome.
                words = DONE if turn.actions else FAILED
                await self._speak_sentence(turn, words.get(turn.lang, words["en"]))
        if fallbacks:
            self.send({"type": "error", "stage": "llm", "msg": fallbacks[0], "fallback": "ollama"})
        self._finish_turn(turn)

    def _finish_turn(self, turn: Turn) -> None:
        turn.done = True
        if self.current is turn and not turn.replied:
            if turn.question:
                # Only the question: a preamble like "fatto" before it would be a false claim.
                self.history.append({"role": "assistant", "content": turn.question})
                turn.replied = True
            elif turn.sentences:
                self.history.append({"role": "assistant", "content": " ".join(turn.sentences)})
                turn.replied = True
            elif turn.in_history:
                del self.history[turn.hist_start:]  # no reply at all (error): drop the exchange
                turn.in_history = False
        turn.mark("total_ms")
        log.info("turn=%d lang=%s %s user=%r actions=%s", turn.id, turn.lang, turn.metrics, turn.user_text, turn.actions)
        if self.svc.stats is not None and turn.user_text is not None:
            self.svc.stats.record(self.source(turn), turn.lang, self.svc.llm.name, len(turn.actions), turn.metrics)
        self.send({"type": "turn_end", "turn": turn.id, "metrics": turn.metrics})

    def source(self, turn: Turn) -> str:
        return "voice" if turn.utt_id else "chat"

    # -- home actions -----------------------------------------------------------------------

    async def _run_tools(self, turn: Turn, calls: list[ToolCall]) -> list[dict]:
        tools = self.svc.tools
        results: list[dict] = []
        risky: list[tuple[ToolCall, str]] = []
        for c in calls:  # in order: "spegni tutto e poi accendi la cucina"
            if turn.superseded or self.current is not turn:
                results.append({"ok": False, "error": "cancelled: the user moved on"})
                continue
            why = tools.risk(c)
            if why:
                risky.append((c, why))
                results.append({"ok": False, "status": "needs_confirmation",
                                "message": "NOT done. The system is asking the user to confirm."})
                continue
            turn.tools_started = True
            # Shielded: a barge-in must not abort a command halfway through.
            results.append(await asyncio.shield(self._exec(turn, c)))
        if risky and not turn.superseded and self.current is turn:
            # All risky actions of the round in one question and one pending action.
            question = confirmation_question([why for _, why in risky], turn.lang)
            self.pending = new_pending([c for c, _ in risky], turn.lang, question, turn.id, self.confirm_ttl_s)
            self.send({"type": "confirm", "turn": turn.id, "action": question})
        return results

    async def _exec(self, turn: Turn, call: ToolCall) -> dict:
        t = time.perf_counter()
        try:
            res = await asyncio.wait_for(self.svc.tools.execute(call), cfg.tool_timeout_s)
        except Exception as e:  # noqa: BLE001 - timeout or transport error
            res = {"ok": False, "error": type(e).__name__}
        if res.get("ok"):
            turn.actions.append(self.svc.tools.summary(call))
        log.info("tool turn=%d %s %s -> %s (%.0f ms)", turn.id, call.name, call.args,
                 str(res)[:200], (time.perf_counter() - t) * 1000)
        return res

    async def _resolve_pending(self, turn: Turn) -> bool:
        """Answer to "are you sure?": "sì" runs the action, "no" cancels, anything else is
        a new request (and the pending action is dropped). No LLM involved."""
        p, self.pending = self.pending, None
        if not p.asked or time.monotonic() >= p.expires_at or turn.user_text is None:
            return False
        verdict = yes_no(turn.user_text)
        if verdict is None:
            return False
        if verdict:
            turn.tools_started = True
            results = [await asyncio.shield(self._exec(turn, c)) for c in p.calls]
            words = DONE if all(r.get("ok") for r in results) else FAILED
        else:
            words = CANCELLED
        await self._speak_sentence(turn, words.get(turn.lang, words["en"]))
        return True

    async def _speak_sentence(self, turn: Turn, raw: str) -> None:
        text = clean_for_tts(raw, turn.lang)
        if not text or self.current is not turn:
            return
        seq = len(turn.sentences)
        turn.sentences.append(text)  # record now: it may be heard before synthesis ends
        if not turn.speak:
            self.send({"type": "sentence", "turn": turn.id, "seq": seq, "text": text})
            return
        tts = self.svc.tts
        if tts.supports(turn.lang):
            self.send({"type": "sentence", "turn": turn.id, "seq": seq, "text": text})
            try:
                async for pcm16 in tts.stream(turn.lang, text):
                    turn.mark("first_audio_ms")
                    if not self.send_audio(turn, seq, pcm16):
                        return
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.error("TTS %s failed: %r; browser fallback", turn.lang, e)
        turn.mark("first_audio_ms")
        self.send({"type": "tts_fallback", "turn": turn.id, "seq": seq,
                   "lang": BCP47.get(turn.lang, turn.lang), "text": text})


class HeadlessSession(Session):
    """A conversation without a WebSocket, for one-shot commands (server/phone.py).

    Never speaks (no voice session), so replies are text only; client messages are kept
    in a short buffer for diagnostics instead of being sent anywhere.
    """

    def __init__(self, svc: Services) -> None:
        super().__init__(None, svc)
        self.events: deque[dict] = deque(maxlen=64)

    def send(self, msg: dict) -> None:
        self.events.append(msg)

    def send_audio(self, turn: Turn, seq: int, pcm16: bytes) -> bool:
        return False


def _log_task_error(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("task failed", exc_info=task.exception())
