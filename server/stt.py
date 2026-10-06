"""Speech-to-text providers: Parakeet (MLX, default), mlx-whisper, Groq Whisper."""

from __future__ import annotations

import io
import logging
import time
import wave
from dataclasses import dataclass

import numpy as np

from .config import cfg
from .langid import to_iso
from .workers import STT_EXEC, run_in

log = logging.getLogger("voicebot.stt")

SAMPLE_RATE = 16000


@dataclass
class Transcript:
    text: str
    lang: str | None = None  # ISO 639-1 if the engine detects it acoustically
    no_speech: bool = False  # engine says this is probably not speech
    provider: str = ""


class ParakeetStt:
    name = "parakeet"

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        self.model = None

    def _load(self) -> None:
        # Runs on the STT thread: MLX streams are thread-local.
        if self.model is None:
            from parakeet_mlx import from_pretrained

            self.model = from_pretrained(self.model_id)

    def _transcribe(self, pcm: np.ndarray) -> Transcript:
        import mlx.core as mx
        from parakeet_mlx.audio import get_logmel

        self._load()
        if len(pcm) < self.model.preprocessor_config.hop_length * 2:
            return Transcript("", provider=self.name)
        # float32 audio: get_logmel breaks with bfloat16 input.
        mel = get_logmel(mx.array(pcm.astype(np.float32)), self.model.preprocessor_config)
        return Transcript(self.model.generate(mel)[0].text.strip(), provider=self.name)

    def _warmup(self) -> None:
        self._load()
        # Noise of a few typical lengths: compiles the kernels real utterances will hit.
        rng = np.random.default_rng(0)
        for seconds in (1, 3, 6):
            self._transcribe((rng.standard_normal(SAMPLE_RATE * seconds) * 0.05).astype(np.float32))

    async def warmup(self) -> None:
        await run_in(STT_EXEC, self._warmup)

    async def transcribe(self, pcm: np.ndarray) -> Transcript:
        return await run_in(STT_EXEC, self._transcribe, pcm)

    async def tick(self) -> None:
        """Tiny inference to bring the GPU out of idle before the real utterance arrives."""
        await run_in(STT_EXEC, self._transcribe, np.zeros(SAMPLE_RATE // 2, dtype=np.float32))


class WhisperMlxStt:
    name = "whisper-mlx"

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id

    def _transcribe(self, pcm: np.ndarray) -> Transcript:
        import mlx_whisper

        r = mlx_whisper.transcribe(
            pcm.astype(np.float32),
            path_or_hf_repo=self.model_id,
            language=None,
            temperature=0.0,
            condition_on_previous_text=False,
            without_timestamps=True,
            no_speech_threshold=0.6,
        )
        segs = r.get("segments") or []
        no_speech = bool(segs) and all(s.get("no_speech_prob", 0) > 0.6 for s in segs)
        return Transcript(r["text"].strip(), to_iso(r.get("language")), no_speech, self.name)

    async def warmup(self) -> None:
        await run_in(STT_EXEC, self._transcribe, np.zeros(SAMPLE_RATE, dtype=np.float32))

    async def transcribe(self, pcm: np.ndarray) -> Transcript:
        return await run_in(STT_EXEC, self._transcribe, pcm)


def pcm_to_wav(pcm: np.ndarray, rate: int = SAMPLE_RATE) -> bytes:
    i16 = (np.clip(pcm, -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(i16.tobytes())
    return buf.getvalue()


class GroqStt:
    name = "groq"

    def __init__(self) -> None:
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=cfg.groq_api_key, base_url=cfg.groq_base_url, timeout=4.0, max_retries=0)

    async def warmup(self) -> None:
        pass

    async def transcribe(self, pcm: np.ndarray) -> Transcript:
        r = await self.client.audio.transcriptions.create(
            file=("utterance.wav", pcm_to_wav(pcm), "audio/wav"),
            model=cfg.groq_stt_model,
            response_format="verbose_json",
            temperature=0.0,
        )
        extra = r.model_extra or {}
        segs = extra.get("segments") or getattr(r, "segments", None) or []

        def g(s, k, d=0.0):
            return s.get(k, d) if isinstance(s, dict) else getattr(s, k, d)

        no_speech = bool(segs) and all(g(s, "no_speech_prob") > 0.6 and g(s, "avg_logprob") < -1.0 for s in segs)
        lang = to_iso(getattr(r, "language", None) or extra.get("language"))
        return Transcript(r.text.strip(), lang, no_speech, self.name)


class SttRouter:
    """Primary provider with automatic fallback to local Parakeet (e.g. Groq 429)."""

    def __init__(self) -> None:
        self.local = ParakeetStt(cfg.stt_model)
        if cfg.stt_provider == "groq" and cfg.groq_api_key:
            self.primary = GroqStt()
        elif cfg.stt_provider == "whisper-mlx":
            self.primary = WhisperMlxStt(cfg.whisper_model)
        else:
            self.primary = self.local
        self.cooldown_until = 0.0
        self.last_fallback: str | None = None

    @property
    def name(self) -> str:
        return self.primary.name

    async def warmup(self) -> None:
        await self.local.warmup()
        if self.primary is not self.local:
            await self.primary.warmup()

    async def transcribe(self, pcm: np.ndarray) -> Transcript:
        self.last_fallback = None
        if self.primary is self.local or time.monotonic() < self.cooldown_until:
            return await self.local.transcribe(pcm)
        try:
            return await self.primary.transcribe(pcm)
        except Exception as e:  # noqa: BLE001
            if not cfg.provider_fallback:
                raise
            log.warning("STT %s failed (%s), falling back to parakeet", self.primary.name, e)
            self.cooldown_until = time.monotonic() + cfg.groq_cooldown_s
            self.last_fallback = f"{self.primary.name}: {type(e).__name__}"
            return await self.local.transcribe(pcm)
