"""Cross-platform behaviour (Windows x64 vs Apple Silicon), checked from any OS."""

import builtins
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from server import __main__ as entry
from server import config, stt
from server.config import POCKET_LANG_NAMES, Config, cfg, default_llm_model, resolve_stt_backend

ROOT = Path(__file__).resolve().parent.parent


def run_py(code: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], check=False, cwd=ROOT, capture_output=True, text=True,
                          timeout=120, env={**os.environ, "PYTHONPATH": str(ROOT), **env})


def test_server_imports_without_mlx():
    r = run_py(
        "import sys\n"
        "for m in ('mlx', 'mlx.core', 'parakeet_mlx', 'parakeet_mlx.audio', 'mlx_whisper'): sys.modules[m] = None\n"
        "import server.main, server.stt\n"
        "assert type(server.stt.make_local_stt()).__name__ == 'ParakeetOnnxStt'\n",
        STT_BACKEND="onnx", STT_MODEL="",
    )
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize(("value", "apple", "expected"), [
    ("auto", True, "mlx"), ("auto", False, "onnx"), ("", False, "onnx"),
    ("ONNX", True, "onnx"), ("mlx", False, "mlx"),
])
def test_stt_backend_resolution(value, apple, expected):
    assert resolve_stt_backend(value, apple) == expected


def test_unknown_stt_backend_is_refused():
    with pytest.raises(ValueError, match="STT_BACKEND"):
        resolve_stt_backend("cuda", False)


def test_llm_model_default_per_platform():
    assert default_llm_model(True) == "qwen3.6:35b-a3b-nvfp4"
    assert default_llm_model(False) == "qwen3.6:35b-a3b"


@pytest.mark.parametrize(("backend", "model"), [
    ("mlx", "mlx-community/parakeet-tdt-0.6b-v3"), ("onnx", "nemo-parakeet-tdt-0.6b-v3"),
])
def test_stt_model_default_follows_backend(monkeypatch, backend, model):
    monkeypatch.setenv("STT_BACKEND", backend)
    monkeypatch.delenv("STT_MODEL", raising=False)
    monkeypatch.delenv("STT_THREADS", raising=False)
    c = Config()
    assert (c.stt_backend, c.stt_model) == (backend, model)
    assert 1 <= c.stt_threads <= 4
    monkeypatch.setenv("STT_MODEL", "my/model")
    monkeypatch.setenv("STT_THREADS", "7")
    assert (Config().stt_model, Config().stt_threads) == ("my/model", 7)


def test_auto_backend_on_this_machine(monkeypatch):
    monkeypatch.setenv("STT_BACKEND", "auto")
    assert Config().stt_backend == ("mlx" if config.IS_APPLE_SILICON else "onnx")


@pytest.mark.parametrize(("backend", "cls"), [("mlx", stt.ParakeetStt), ("onnx", stt.ParakeetOnnxStt)])
def test_make_local_stt(monkeypatch, backend, cls):
    monkeypatch.setattr(cfg, "stt_backend", backend)
    local = stt.make_local_stt()
    assert type(local) is cls and local.name == "parakeet" and local.model is None  # loads lazily


def test_onnx_stt_maps_the_mlx_model_id():
    assert stt.ParakeetOnnxStt("mlx-community/parakeet-tdt-0.6b-v3").model_id == "nemo-parakeet-tdt-0.6b-v3"
    assert stt.ParakeetOnnxStt("nemo-parakeet-tdt-0.6b-v2").model_id == "nemo-parakeet-tdt-0.6b-v2"


async def test_onnx_stt_short_audio_and_tick_do_not_infer():
    s = stt.ParakeetOnnxStt("nemo-parakeet-tdt-0.6b-v3")
    s.model = object()  # any real inference would fail on this
    assert s._transcribe(stt.np.zeros(100, dtype=stt.np.float32)).text == ""
    await s.tick()


def test_whisper_mlx_off_apple_falls_back_to_local(monkeypatch):
    monkeypatch.setattr(stt, "IS_APPLE_SILICON", False)
    monkeypatch.setattr(cfg, "stt_provider", "whisper-mlx")
    monkeypatch.setattr(cfg, "stt_backend", "onnx")
    router = stt.SttRouter()
    assert router.primary is router.local and router.name == "parakeet"


STATIC = {
    "/app.js": "text/javascript", "/viz.js": "text/javascript", "/player-worklet.js": "text/javascript",
    "/sw.js": "text/javascript", "/vendor/ort-wasm-simd-threaded.mjs": "text/javascript",
    "/vendor/vad.worklet.bundle.min.js": "text/javascript", "/vendor/ort-wasm-simd-threaded.wasm": "application/wasm",
    "/vendor/silero_vad_v5.onnx": "application/octet-stream", "/manifest.json": "application/json",
    "/style.css": "text/css", "/favicon.svg": "image/svg+xml", "/": "text/html",
}


def test_static_mime_types_survive_a_poisoned_registry():
    # Windows mimetypes reads the registry, where .js is often text/plain: emulate it before the app import.
    r = run_py(
        "import json, mimetypes\n"
        "for ext in ('.js', '.mjs', '.wasm', '.json', '.css'): mimetypes.add_type('text/plain', ext)\n"
        "from fastapi.testclient import TestClient\n"
        "from server.main import app\n"
        "c = TestClient(app)\n"  # no lifespan: static files need no models
        f"print(json.dumps({{p: c.get(p).headers['content-type'].split(';')[0] for p in {list(STATIC)!r}}}))\n",
        LOCAL_HOSTS="127.0.0.1,localhost,testserver", STT_BACKEND="onnx",
    )
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1]) == STATIC


def test_bind_refuses_a_busy_port_cleanly(monkeypatch, capsys):
    busy = entry.bind("127.0.0.1", 0)
    busy.listen()
    port = busy.getsockname()[1]
    free = entry.bind("127.0.0.1", 0)
    free_port = free.getsockname()[1]
    free.close()
    try:
        with pytest.raises(OSError):
            entry.bind("127.0.0.1", port)
        assert entry.bind_all("127.0.0.1", [free_port, port]) is None
        assert "Cannot listen on 127.0.0.1:" in capsys.readouterr().err
        entry.bind("127.0.0.1", free_port).close()  # the first socket was released
        monkeypatch.setattr(cfg, "host", "127.0.0.1")
        monkeypatch.setattr(cfg, "port", port)
        monkeypatch.setattr(cfg, "remote_enabled", False)
        monkeypatch.setattr(cfg, "api_allow_risky", False)
        assert entry.main() == 2
    finally:
        busy.close()


def test_bind_sets_reuseaddr_only_on_posix():
    s = entry.bind("127.0.0.1", 0)
    try:
        assert bool(s.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)) is (sys.platform != "win32")
    finally:
        s.close()


def test_pocket_configs_load_as_utf8_under_a_cp1252_locale(monkeypatch):
    pcfg = pytest.importorskip("pocket_tts.utils.config")
    from server.tts import utf8_pocket_configs

    real_open = builtins.open

    def cp1252_open(file, mode="r", *args, encoding=None, **kw):  # what a bare open() does on Windows
        if "b" not in mode and encoding is None:
            encoding = "cp1252"
        return real_open(file, mode, *args, encoding=encoding, **kw)

    monkeypatch.setattr(builtins, "open", cp1252_open)
    monkeypatch.setattr(pcfg, "open", cp1252_open, raising=False)  # undone after the test
    italian = pcfg.CONFIGS_DIR / "italian.yaml"
    with pytest.raises(UnicodeDecodeError):
        pcfg.load_config(italian)
    utf8_pocket_configs()
    for name in POCKET_LANG_NAMES.values():
        pcfg.load_config(pcfg.CONFIGS_DIR / f"{name}.yaml")
