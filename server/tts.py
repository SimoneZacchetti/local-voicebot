"""Text-to-speech: Kyutai Pocket TTS, one dedicated model per language, streamed.

Languages without a Pocket model are spoken by the browser (speechSynthesis).
"""

from __future__ import annotations

import builtins
import functools
import logging
import threading
import time
from collections.abc import AsyncIterator

import numpy as np

from .config import POCKET_LANG_NAMES, cfg
from .workers import TTS_EXEC, run_in, stream_in

log = logging.getLogger("voicebot.tts")

SAMPLE_RATE = 24000


def utf8_pocket_configs() -> None:
    """pocket_tts reads its language YAMLs with a bare open(), i.e. the locale encoding: cp1252 on Windows
    fails on the UTF-8 quotes in italian/french/german/... configs. Shadow open in that module only."""
    import pocket_tts.utils.config as pcfg

    if getattr(pcfg, "open", builtins.open) is builtins.open:
        pcfg.open = functools.partial(builtins.open, encoding="utf-8")


class PocketTts:
    def __init__(self) -> None:
        self.models: dict[str, object] = {}
        self.states: dict[str, object] = {}
        self.failed: set[str] = set()

    def supports(self, lang: str) -> bool:
        return lang in POCKET_LANG_NAMES and lang not in self.failed

    # All methods below run on the TTS thread.

    def _ensure(self, lang: str) -> None:
        if lang in self.models:
            return
        from pocket_tts import TTSModel

        utf8_pocket_configs()
        t = time.perf_counter()
        try:
            model = TTSModel.load_model(language=POCKET_LANG_NAMES[lang], quantize=cfg.pocket_quantize)
            state = model.get_state_for_audio_prompt(cfg.voices[lang])
        except Exception:
            self.failed.add(lang)
            raise
        self.models[lang], self.states[lang] = model, state
        log.info("pocket %s (%s) loaded in %.1fs", lang, cfg.voices[lang], time.perf_counter() - t)

    def _preload(self, langs: list[str]) -> None:
        for lang in langs:
            if lang not in POCKET_LANG_NAMES:
                continue
            try:
                self._ensure(lang)
                audio = np.concatenate([c.numpy() for c in self._gen(lang, "Ok, pronto.", threading.Event())])
                if not np.abs(audio).max() > 0.01:
                    raise RuntimeError("warm-up produced silence")
            except Exception as e:  # noqa: BLE001
                self.failed.add(lang)
                log.error("pocket %s unavailable: %s", lang, e)

    def _gen(self, lang: str, text: str, stop: threading.Event):
        self._ensure(lang)
        return self.models[lang].generate_audio_stream(self.states[lang], text, stop=stop)

    def _pcm16_stream(self, lang: str, text: str, stop: threading.Event):
        for chunk in self._gen(lang, text, stop):
            a = chunk.clamp(-1, 1).numpy()
            yield (a * 32767).astype("<i2").tobytes()

    async def preload(self, langs: list[str]) -> None:
        await run_in(TTS_EXEC, self._preload, langs)

    async def stream(self, lang: str, text: str) -> AsyncIterator[bytes]:
        async for b in stream_in(TTS_EXEC, lambda stop: self._pcm16_stream(lang, text, stop)):
            yield b
