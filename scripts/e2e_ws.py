"""End-to-end check against a running server, without a browser.

Synthesizes spoken questions with Pocket TTS, sends them over the WebSocket like the
browser does, then transcribes the bot's spoken reply with Parakeet and prints latencies.

Usage: uv run python scripts/e2e_ws.py [ws://127.0.0.1:8000/ws]
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import numpy as np
import websockets
from scipy.signal import resample_poly

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.session import AUDIO_HEADER, UTT_HEADER

QUESTIONS = [
    ("italian", "giovanni", "Ciao! Mi consigli un posto carino da visitare in Toscana?"),
    ("english", "alba", "Hello! What is a good way to learn a new language quickly?"),
    ("italian", "giovanni", "Quanto fa diciassette per ventitré?"),
    ("french", "estelle", "Bonjour ! Quelle est la capitale de l'Australie ?"),
]


def synth(models: dict, lang: str, voice: str, text: str) -> np.ndarray:
    from pocket_tts import TTSModel

    if lang not in models:
        m = TTSModel.load_model(language=lang)
        models[lang] = (m, m.get_state_for_audio_prompt(voice))
    m, st = models[lang]
    a = m.generate_audio(st, text).numpy().astype(np.float32)
    return resample_poly(a, 2, 3).astype(np.float32)  # 24k -> 16k


async def main(url: str) -> None:
    from server.stt import make_local_stt

    stt = make_local_stt()
    models: dict = {}
    async with websockets.connect(url, max_size=None) as ws:
        ready = json.loads(await ws.recv())
        print("ready:", ready["providers"])
        last_turn, last_seq = -1, -1
        for i, (lang, voice, text) in enumerate(QUESTIONS, start=1):
            pcm = synth(models, lang, voice, text)
            i16 = (np.clip(pcm, -1, 1) * 32767).astype("<i2")
            t0 = time.perf_counter()
            await ws.send(UTT_HEADER.pack(i, last_turn, last_seq, 0) + i16.tobytes())
            audio, turn, first_audio, transcript, metrics, seq = [], None, None, None, {}, -1
            while True:
                msg = await ws.recv()
                if isinstance(msg, bytes):
                    t, seq = AUDIO_HEADER.unpack_from(msg)
                    if t == turn:
                        first_audio = first_audio or time.perf_counter() - t0
                        audio.append(np.frombuffer(msg, "<i2", offset=AUDIO_HEADER.size))
                    continue
                m = json.loads(msg)
                if m["type"] == "turn_start":
                    turn = m["turn"]
                elif m["type"] == "transcript":
                    transcript = m
                elif m["type"] in ("skip", "error"):
                    print("  !!", m)
                    if m["type"] == "skip":
                        break
                elif m["type"] == "turn_end" and m["turn"] == turn:
                    metrics = m["metrics"]
                    last_turn, last_seq = turn, seq  # we "heard" the whole reply
                    break
            if not audio:
                continue
            reply = np.concatenate(audio).astype(np.float32) / 32768
            heard = stt._transcribe(resample_poly(reply, 2, 3).astype(np.float32)).text
            print(f"\nQ[{transcript['lang']}] {transcript['text']}")
            print(f"A (heard back) {heard}")
            print(f"  client first audio {first_audio * 1000:.0f} ms | server {metrics} | reply {len(reply) / 24000:.1f}s")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8000/ws"))
