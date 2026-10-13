"""End-to-end home control check: spoken commands -> voicebot -> Home Assistant.

Speaks each command with Pocket TTS, sends it like the browser does, prints the tool calls,
the spoken reply (transcribed back) and verifies the resulting state in Home Assistant.

Usage: HA_URL=... HA_TOKEN=... uv run python scripts/e2e_home.py [ws://127.0.0.1:8000/ws]
(the voicebot server must run with HOME_BACKEND=ha against the same Home Assistant)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
import numpy as np
import websockets
from scipy.signal import resample_poly

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.session import AUDIO_HEADER, UTT_HEADER

HA_URL = os.environ.get("HA_URL", "http://127.0.0.1:18123")
HA = httpx.Client(base_url=HA_URL, headers={"Authorization": f"Bearer {os.environ['HA_TOKEN']}"})


def state(eid: str) -> dict:
    return HA.get(f"/api/states/{eid}").json()


def setup() -> None:
    HA.post("/api/services/light/turn_on", json={"entity_id": "light.kitchen_lights"})
    HA.post("/api/services/light/turn_off", json={"entity_id": "light.bed_light"})
    HA.post("/api/services/lock/lock", json={"entity_id": "lock.front_door"})
    HA.post("/api/services/switch/turn_on", json={"entity_id": "switch.decorative_lights"})
    HA.post("/api/services/cover/close_cover", json={"entity_id": "cover.garage_door"})
    HA.post("/api/services/media_player/volume_set", json={"entity_id": "media_player.living_room", "volume_level": 0.8})
    time.sleep(0.5)


# (spoken command, check(reply_text) -> (ok, detail))
SCENARIO = [
    ("Spegni la luce della cucina.",
     lambda r: (state("light.kitchen_lights")["state"] == "off", state("light.kitchen_lights")["state"])),
    ("La luce della cucina è accesa?",
     lambda r: ("spent" in r.lower() or "no" in r.lower().split(), r)),
    ("Accendi la luce della camera da letto al cinquanta per cento.",
     lambda r: ((s := state("light.bed_light"))["state"] == "on" and abs((s["attributes"].get("brightness") or 0) - 128) <= 3,
                f"{s['state']} brightness={s['attributes'].get('brightness')}")),
    ("Spegni la presa delle luci decorative.",
     lambda r: (state("switch.decorative_lights")["state"] == "off", state("switch.decorative_lights")["state"])),
    ("Apri il basculante del garage.",
     lambda r: (state("cover.garage_door")["state"] == "closed" and "apri" in r.lower(),
                f"must still be closed and ask to open: {state('cover.garage_door')['state']}")),
    ("Sì, confermo.",
     lambda r: (state("cover.garage_door")["state"] in ("open", "opening"), state("cover.garage_door")["state"])),
    ("Sblocca la serratura Front Door.",
     lambda r: (state("lock.front_door")["state"] == "locked" and ("sblocc" in r.lower() or "chiave" in r.lower()),
                f"locked={state('lock.front_door')['state']} question matches the action")),
    ("No, lascia stare.",
     lambda r: (state("lock.front_door")["state"] == "locked", state("lock.front_door")["state"])),
    ("Che capitale ha la Francia?",
     lambda r: ("parigi" in r.lower(), r)),
]


async def main(url: str) -> int:
    import mlx.core as mx
    from parakeet_mlx import from_pretrained
    from parakeet_mlx.audio import get_logmel
    from pocket_tts import TTSModel

    setup()
    tts = TTSModel.load_model(language="italian")
    voice = tts.get_state_for_audio_prompt("giovanni")
    stt = from_pretrained("mlx-community/parakeet-tdt-0.6b-v3")
    failures = 0
    async with websockets.connect(url, max_size=None) as ws:
        ready = json.loads(await ws.recv())
        print("ready:", ready["providers"])
        last_turn, last_seq = -1, -1
        for i, (text, check) in enumerate(SCENARIO, start=1):
            pcm = resample_poly(tts.generate_audio(voice, text).numpy().astype(np.float32), 2, 3)
            t0 = time.perf_counter()
            await ws.send(UTT_HEADER.pack(i, last_turn, last_seq, 0) + (np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())
            audio, turn, seq, tools, confirm, first = [], None, -1, [], None, None
            while True:
                msg = await ws.recv()
                if isinstance(msg, bytes):
                    t, seq = AUDIO_HEADER.unpack_from(msg)
                    if t == turn:
                        first = first or time.perf_counter() - t0
                        audio.append(np.frombuffer(msg, "<i2", offset=AUDIO_HEADER.size))
                    continue
                m = json.loads(msg)
                if m["type"] == "turn_start":
                    turn = m["turn"]
                elif m["type"] == "transcript":
                    heard_q = m["text"]
                elif m["type"] == "tool":
                    tools += [f"{c['name'].split('__')[-1]}{json.dumps(c['args'], ensure_ascii=False)}" for c in m["calls"]]
                elif m["type"] == "confirm":
                    confirm = m["action"]
                elif m["type"] in ("skip", "error"):
                    print("  !!", m)
                elif m["type"] == "turn_end" and m["turn"] == turn:
                    last_turn, last_seq = turn, seq
                    break
            reply = ""
            if audio:
                a = np.concatenate(audio).astype(np.float32) / 32768
                reply = stt.generate(get_logmel(mx.array(resample_poly(a, 2, 3).astype(np.float32)), stt.preprocessor_config))[0].text
            await asyncio.sleep(0.3)
            ok, detail = check(reply)
            claims = any(w in reply.lower() for w in ("fatto", "ho acceso", "ho spento", "ho sbloccato", "ho aperto", "ho chiuso"))
            if claims and not tools and not text.lower().startswith(("sì", "si")):
                ok, detail = False, f"claims an action without any tool call: {detail}"
            failures += not ok
            print(f"\n[{'OK' if ok else 'FAIL'}] {heard_q}")
            print(f"  tools: {tools or '-'}{'  confirm: ' + confirm if confirm else ''}")
            print(f"  reply: {reply}")
            print(f"  first audio {first * 1000 if first else -1:.0f} ms | check: {detail}")
    print(f"\n{len(SCENARIO) - failures}/{len(SCENARIO)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8000/ws")))
