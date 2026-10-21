"""Browser end-to-end test: real Chrome (or Edge), real VAD, fake microphone fed from a WAV file.

Synthesizes a spoken question, feeds it to Chrome as the microphone, clicks Start and
waits for the bot's transcript, reply and audio playback.

Usage: uv run python scripts/e2e_browser.py [http://127.0.0.1:8000] [--headed]
"""

from __future__ import annotations

import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
from playwright.sync_api import sync_playwright

# (start second, text). Silence first so the greeting plays; the file loops, so end with silence.
SCENARIOS = {
    "simple": [(5, "Ciao! Qual è la montagna più alta d'Italia?")],
    # Second utterance lands while the bot is still telling the story: barge-in.
    "barge": [(5, "Raccontami una storia lunga su un drago e un cavaliere, con tanti dettagli."),
              (14, "Basta così, grazie. Dimmi solo che ore sono a Tokyo quando a Roma è mezzogiorno.")],
}


def make_wav(path: Path, scenario: str) -> None:
    from pocket_tts import TTSModel

    m = TTSModel.load_model(language="italian")
    st = m.get_state_for_audio_prompt("giovanni")
    sr = m.sample_rate
    pcm = np.zeros(sr * 60, dtype=np.float32)
    for start, text in SCENARIOS[scenario]:
        a = m.generate_audio(st, text).numpy().astype(np.float32) * 0.9
        pcm[int(start * sr) : int(start * sr) + len(a)] = a
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())


def launch(p, headed: bool, wav: Path):
    args = [
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        f"--use-file-for-fake-audio-capture={wav}",
        "--autoplay-policy=no-user-gesture-required",
    ]
    for channel in ("chrome", "msedge"):  # Windows machines often ship only Edge
        try:
            return p.chromium.launch(channel=channel, headless=not headed, args=args)
        except Exception as e:  # noqa: BLE001
            err = e
    raise err


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    url = args[0] if args else "http://127.0.0.1:8000"
    headed = "--headed" in sys.argv
    scenario = "barge" if "--barge" in sys.argv else "simple"
    wav = Path(tempfile.gettempdir()) / f"voicebot_{scenario}.wav"
    make_wav(wav, scenario)

    with sync_playwright() as p:
        browser = launch(p, headed, wav)
        page = browser.new_page()
        logs: list[str] = []
        page.on("console", lambda m: logs.append(f"[{m.type}] {m.text}"))
        page.on("pageerror", lambda e: logs.append(f"[pageerror] {e}"))
        page.goto(url)
        page.click("#start")
        page.click("#start", force=True)  # double click during startup must not start twice
        ok = True
        if scenario == "barge":
            ok = run_barge(page)
            print("console:", *logs[-15:], sep="\n  ")
            browser.close()
            return 0 if ok else 1
        try:
            page.wait_for_selector(".msg.bot", state="attached", timeout=30000)
            print("greeting:", page.locator(".msg.bot").first.text_content())
            page.wait_for_selector(".msg.user", state="attached", timeout=30000)
            print("user    :", page.locator(".msg.user").first.text_content())
            page.wait_for_function("document.querySelectorAll('.msg.bot').length >= 2 && document.querySelectorAll('.msg.bot')[1].textContent.length > 10", timeout=30000)
            page.wait_for_function("document.getElementById('latency').textContent.length > 0", timeout=15000)
            page.wait_for_timeout(1500)
            print("bot     :", page.locator(".msg.bot").nth(1).text_content())
            print("latency :", page.locator("#latency").text_content(), "|", page.locator("#latency").get_attribute("title"))
            print("status  :", page.locator("#status").text_content())
            print("greetings:", page.locator(".msg.bot").all_text_contents().count("itCiao! Sono pronto, dimmi pure."))
        except Exception as e:  # noqa: BLE001
            ok = False
            print("FAILED:", e)
            print("log html:", page.locator("#log").inner_html()[:1500])
            print("status  :", page.locator("#status").text_content())
            print("greetings:", page.locator(".msg.bot").all_text_contents().count("itCiao! Sono pronto, dimmi pure."))
        errors = [l for l in logs if "error" in l.lower()]
        print("console:", *logs[-15:], sep="\n  ")
        browser.close()
    return 0 if ok and not errors else 1


def run_barge(page) -> bool:
    try:
        page.wait_for_function("document.querySelectorAll('.msg.user').length >= 2", timeout=40000)
        # The second utterance has a pause after "grazie.": its two halves must end up merged.
        page.wait_for_function("[...document.querySelectorAll('.msg.user')].some(e => e.textContent.includes('mezzogiorno'))", timeout=40000)
        page.wait_for_function("document.querySelectorAll('.msg.bot').length >= 3 && [...document.querySelectorAll('.msg.bot')].at(-1).textContent.length > 10", timeout=30000)
        page.wait_for_timeout(2500)
    except Exception as e:  # noqa: BLE001
        print("FAILED:", e)
        print(page.locator("#log").inner_html()[:3000])
        return False
    users = page.locator(".msg.user").all_text_contents()
    bots = page.locator(".msg.bot")
    story = bots.nth(1)
    print("user 1 :", users[0])
    print("bot 1  :", story.text_content()[:200], "...")
    print("interrupted class:", "interrupted" in (story.get_attribute("class") or ""))
    print("user 2 :", users[1])
    print("bot 2  :", bots.last.text_content())
    return "interrupted" in (story.get_attribute("class") or "") and len(users) == 2 and "mezzogiorno" in users[1]


if __name__ == "__main__":
    sys.exit(main())
