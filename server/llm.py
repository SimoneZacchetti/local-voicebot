"""Streaming LLM client: Ollama (default) or Groq, with tool calling.

``stream()`` yields text deltas (str) and complete tool calls (ToolCall). Messages use one
neutral, OpenAI-like shape; each provider converts them at request time:
- assistant tool calls: {"role": "assistant", "content": "", "tool_calls": [{"id", "name", "args"}]}
- tool results:         {"role": "tool", "tool_call_id", "name", "content"}
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import AsyncIterator

import httpx
from openai import AsyncOpenAI

from .config import cfg
from .tools import ToolCall

log = logging.getLogger("voicebot.llm")

Delta = str | ToolCall

# Words that announce a question worth reasoning about, in the 7 supported languages.
_HARD = re.compile(
    r"\b(perch[eé]|come mai|spiega|calcola|quanto fa|confronta|differenz|ragiona|dimostra|risolvi|analizza|"
    r"conviene|pro e contro|why|explain|calculate|compute|how much is|compare|difference|reason|prove|solve|"
    r"analy[sz]e|pros and cons|step by step|pourquoi|expliqu|calcul|compar|diff[ée]rence|warum|erkl[äa]r|"
    r"berechne|vergleich|unterschied|por ?qu[eé]|porqu[eê]|explica|diferen[cç]a|waarom|leg uit|bereken|"
    r"vergelijk|verschil)",
    re.IGNORECASE,
)
_MATH = re.compile(r"\d\s*[-+*/x×÷^]\s*\d")


def wants_thinking(messages: list[dict]) -> bool:
    """Reason only on a fresh user question that looks hard: reasoning costs 1-3 s before
    the first word, too much for small talk and home commands."""
    if cfg.llm_think in ("off", "always"):
        return cfg.llm_think == "always"
    last = messages[-1] if messages else {}
    if last.get("role") != "user":
        return False  # tool results: just report them
    text = str(last.get("content", ""))
    return bool(_HARD.search(text) or _MATH.search(text))


def to_ollama(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content", ""), "tool_calls": [
                {"function": {"name": c["name"], "arguments": c["args"]}} for c in m["tool_calls"]]})
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_name": m["name"], "content": m["content"]})
        else:
            out.append(m)
    return out


def to_openai(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content") or None, "tool_calls": [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": json.dumps(c["args"], ensure_ascii=False)}}
                for c in m["tool_calls"]]})
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        else:
            out.append(m)
    return out


class OllamaLlm:
    """Uses Ollama's native /api/chat: it honours ``think: false`` and ``keep_alive``,
    which the OpenAI-compatible endpoint does not."""

    name = "ollama"

    def __init__(self) -> None:
        self.root = cfg.llm_base_url.removesuffix("/").removesuffix("/v1")
        self.client = httpx.AsyncClient(base_url=self.root, timeout=httpx.Timeout(60.0, connect=3.0))

    def _body(self, messages: list[dict], tools: list[dict], think: bool = False, **extra) -> dict:
        body = {"model": cfg.llm_model, "messages": to_ollama(messages), "think": think, "keep_alive": -1, **extra}
        if tools:
            body["tools"] = tools  # identical on every call: keeps the prompt cache warm
        return body

    async def warmup(self) -> None:
        from .prompts import build_messages

        await self.prefill(build_messages([{"role": "user", "content": "Ciao!"}], "it"))

    async def prefill(self, messages: list[dict], tools: list[dict] | None = None) -> None:
        """Generate one token for ``messages``: loads the model, wakes the GPU from idle
        clocks and puts the conversation prefix in the prompt cache."""
        r = await self.client.post("/api/chat", json=self._body(
            messages, tools or [], stream=False, options={"num_predict": 1, "temperature": cfg.llm_temperature}))
        r.raise_for_status()

    async def stream(self, messages: list[dict], tools: list[dict] | None = None) -> AsyncIterator[Delta]:
        if not wants_thinking(messages):
            async for d in self._stream(messages, tools, think=False):
                yield d
            return
        log.info("thinking on this question")
        notes: list[str] = []
        async for d in self._stream(messages, tools, think=True, notes=notes):
            yield d
        if len(notes) < cfg.llm_think_budget:
            return
        # The model got lost in its reasoning (it loops on the format rules) before saying
        # anything: answer without thinking, from the notes it took so far.
        log.warning("thinking budget of %d tokens used up, answering without it", cfg.llm_think_budget)
        msgs = [dict(m) for m in messages]
        msgs[-1]["content"] += f"\n\n(Your notes so far, use them and answer now: {''.join(notes)})"
        async for d in self._stream(msgs, tools, think=False):
            yield d

    async def _stream(self, messages: list[dict], tools: list[dict] | None, think: bool,
                      notes: list[str] | None = None) -> AsyncIterator[Delta]:
        """With ``think``, reasoning chunks go to ``notes``; the stream stops as soon as they
        reach ``LLM_THINK_BUDGET`` (one chunk is one token) without any answer yet."""
        body = self._body(messages, tools or [], think=think, stream=True, options={
            "num_predict": cfg.llm_max_tokens + (cfg.llm_think_budget if think else 0),
            "temperature": cfg.llm_temperature})
        async with self.client.stream("POST", "/api/chat", json=body) as r:
            if r.status_code != 200:
                raise RuntimeError(f"ollama HTTP {r.status_code}: {(await r.aread())[:300]!r}")
            n = 0
            answered = False
            async for line in r.aiter_lines():
                if not line:
                    continue
                data = json.loads(line)
                if "error" in data:
                    raise RuntimeError(f"ollama: {data['error']}")
                msg = data.get("message") or {}
                if notes is not None and msg.get("thinking") and not answered:
                    notes.append(msg["thinking"])
                    if len(notes) >= cfg.llm_think_budget:
                        return
                if msg.get("content"):
                    answered = True
                    yield msg["content"]
                # Each tool call arrives whole, in its own chunk (done=false).
                for c in msg.get("tool_calls") or []:
                    fn = c.get("function") or {}
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        args = json.loads(args or "{}")
                    n += 1
                    answered = True
                    yield ToolCall(c.get("id") or f"call_{n}", fn.get("name", ""), args)
                if data.get("done"):
                    break


class GroqLlm:
    name = "groq"

    def __init__(self) -> None:
        self.client = AsyncOpenAI(api_key=cfg.groq_api_key, base_url=cfg.groq_base_url, timeout=10.0, max_retries=0)

    async def stream(self, messages: list[dict], tools: list[dict] | None = None) -> AsyncIterator[Delta]:
        extra = {"tools": tools, "tool_choice": "auto", "parallel_tool_calls": True} if tools else {}
        stream = await self.client.chat.completions.create(
            model=cfg.groq_llm_model,
            messages=to_openai(messages),
            stream=True,
            max_tokens=cfg.llm_max_tokens,
            temperature=cfg.llm_temperature,
            reasoning_effort="none",
            **extra,
        )
        pending: dict[int, dict] = {}  # tool call fragments by index
        try:
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    yield delta.content
                for tc in (delta.tool_calls or []) if delta else []:
                    p = pending.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    p["id"] = tc.id or p["id"]
                    if tc.function:
                        p["name"] += tc.function.name or ""
                        p["args"] += tc.function.arguments or ""
        finally:
            await stream.close()
        for i, p in sorted(pending.items()):
            yield ToolCall(p["id"] or f"call_{i}", p["name"], json.loads(p["args"] or "{}"))


class LlmRouter:
    def __init__(self) -> None:
        self.local = OllamaLlm()
        self.primary = GroqLlm() if cfg.llm_provider == "groq" and cfg.groq_api_key else self.local
        self.cooldown_until = 0.0
        self.last_fallback: str | None = None
        self.tools: list[dict] = []  # fixed for the whole process (prompt cache)

    @property
    def name(self) -> str:
        return self.primary.name

    @property
    def using_remote(self) -> bool:
        return self.primary is not self.local and time.monotonic() >= self.cooldown_until

    def set_tools(self, tools: list[dict]) -> None:
        self.tools = tools

    async def warmup(self) -> None:
        from .prompts import build_messages

        msgs = build_messages([{"role": "user", "content": "Ciao!"}], "it", with_tools=bool(self.tools))
        await self.local.prefill(msgs, self.tools)

    async def prefill(self, messages: list[dict]) -> None:
        if not self.using_remote:
            await self.local.prefill(messages, self.tools)

    async def stream(self, messages: list[dict]) -> AsyncIterator[Delta]:
        self.last_fallback = None
        if not self.using_remote:
            async for t in self.local.stream(messages, self.tools):
                yield t
            return
        produced = False
        try:
            async for t in self.primary.stream(messages, self.tools):
                produced = True
                yield t
            return
        except Exception as e:
            if produced or not cfg.provider_fallback:
                raise
            log.warning("LLM %s failed (%s), falling back to ollama", self.primary.name, e)
            self.cooldown_until = time.monotonic() + cfg.groq_cooldown_s
            self.last_fallback = f"{self.primary.name}: {type(e).__name__}"
        async for t in self.local.stream(messages, self.tools):
            yield t
