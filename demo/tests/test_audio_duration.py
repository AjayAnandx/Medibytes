"""Regression: maximum audio duration is enforced on DECODED audio, every format.

Run:  python -m pytest demo/tests/test_audio_duration.py -q

Synthetic sine tones encoded with the bundled imageio-ffmpeg (no patient
audio). audio_clean.MAX_SECONDS is lowered per test so files stay a few
seconds long; the code path is the same as for the real 30-minute limit.
Covers: M4A (no header duration) was never checked; an MP3 whose header
understates its length passed the header check; decode is bounded; corrupt
uploads get a clear error; the app rejects before speech recognition.
"""
import os
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO = os.path.dirname(_HERE)
sys.path.insert(0, _DEMO)

import audio_clean  # noqa: E402

imageio_ffmpeg = pytest.importorskip("imageio_ffmpeg")
CODECS = {"wav": ["-c:a", "pcm_s16le"], "mp3": ["-c:a", "libmp3lame"], "m4a": ["-c:a", "aac"],
          "ogg": ["-c:a", "libvorbis"], "flac": ["-c:a", "flac"]}


def _tone(tmp_path, ext, seconds):
    p = tmp_path / f"synthetic_tone_{seconds}s.{ext}"
    r = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi",
                        "-i", f"sine=frequency=440:duration={seconds}", "-ar", "16000", "-ac", "1",
                        *CODECS[ext], str(p)], capture_output=True)
    if r.returncode:
        pytest.skip(f"ffmpeg cannot encode {ext} here")
    return str(p)


@pytest.fixture
def limit(monkeypatch):
    def set_limit(seconds):
        monkeypatch.setattr(audio_clean, "MAX_SECONDS", seconds)
    return set_limit


def test_m4a_over_limit_rejected(tmp_path, limit):
    limit(3)
    with pytest.raises(ValueError) as ei:
        audio_clean.clean_audio(_tone(tmp_path, "m4a", 5), str(tmp_path / "out.wav"))
    assert str(ei.value).startswith("413 TOO_LONG")
    assert not (tmp_path / "out.wav").exists()  # nothing produced for STT


@pytest.mark.parametrize("ext", ["wav", "mp3", "m4a", "ogg", "flac"])
def test_every_format_over_limit_rejected(tmp_path, limit, ext):
    limit(3)
    with pytest.raises(ValueError, match=r"^413 TOO_LONG"):
        audio_clean.clean_audio(_tone(tmp_path, ext, 5), str(tmp_path / "out.wav"))


@pytest.mark.parametrize("ext", ["wav", "mp3", "m4a"])
def test_under_limit_accepted(tmp_path, limit, ext):
    limit(10)
    meta = audio_clean.clean_audio(_tone(tmp_path, ext, 5), str(tmp_path / "out.wav"))
    assert 4.5 <= meta["duration_s"] <= 5.5


def test_mp3_header_understating_length_rejected(tmp_path, limit):
    one = open(_tone(tmp_path, "mp3", 5), "rb").read()
    p = tmp_path / "concat.mp3"
    p.write_bytes(one * 4)  # header/Xing says ~5 s, decodes to ~20 s
    limit(8)
    with pytest.raises(ValueError, match=r"^413 TOO_LONG"):
        audio_clean.clean_audio(str(p), str(tmp_path / "out.wav"))


def test_limit_message_is_clear(tmp_path, limit):
    limit(3)
    with pytest.raises(ValueError) as ei:
        audio_clean.clean_audio(_tone(tmp_path, "m4a", 5), str(tmp_path / "out.wav"))
    msg = str(ei.value)
    assert "limit" in msg and "decoded" in msg


def test_decode_is_bounded(tmp_path):
    y, sr = audio_clean._load_mono_float(_tone(tmp_path, "m4a", 8), max_seconds=2)
    # stops just past the limit (limit + 1 s, so "too long" is detectable)
    # instead of decoding all 8 s
    assert 2 < len(y) / sr <= 3.1


def test_corrupt_upload_clear_error(tmp_path):
    p = tmp_path / "corrupt.m4a"
    p.write_bytes(bytes(range(256)) * 16)
    with pytest.raises(ValueError) as ei:
        audio_clean.clean_audio(str(p), str(tmp_path / "out.wav"))
    msg = str(ei.value)
    assert msg.startswith("422 DECODE_FAIL")
    assert "corrupt or unsupported" in msg
    assert "pip install" not in msg  # ffmpeg is installed; not the problem


# ---- app: over-limit upload rejected before speech recognition ----

def _app_script():
    import os
    import sys

    sys.path.insert(0, os.environ["MB_TEST_DEMO_DIR"])
    import streamlit as st
    import app
    import audio_clean

    class _Up:
        name = "synthetic_long_tone.m4a"

        def __init__(self, c):
            self._c = c

        def getvalue(self):
            return self._c

        def getbuffer(self):
            return memoryview(self._c)

    with open(os.environ["MB_TEST_AUDIO"], "rb") as f:
        up = _Up(f.read())
    calls = app.__dict__.setdefault("_MB_STT_CALLS", [])

    def fake_stt(*a, **k):
        calls.append(1)
        raise AssertionError("speech recognition must not run for over-limit audio")

    saved = (st.file_uploader, app.run_stt_extract, app._ollama_available, app._HERE,
             audio_clean.MAX_SECONDS)
    st.file_uploader = lambda *a, **k: up
    app.run_stt_extract = fake_stt
    app._ollama_available = lambda model: (False, "disabled in test")
    app._HERE = os.environ["MB_TEST_OUT_DIR"]
    audio_clean.MAX_SECONDS = 3
    try:
        app.main()
    finally:
        (st.file_uploader, app.run_stt_extract, app._ollama_available, app._HERE,
         audio_clean.MAX_SECONDS) = saved


def test_app_rejects_long_m4a_before_stt(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    import app
    app.__dict__["_MB_STT_CALLS"] = []
    out = tmp_path / "out"
    for d in ("entities", "exports", "transcripts"):
        (out / d).mkdir(parents=True)
    monkeypatch.setenv("MB_TEST_DEMO_DIR", _DEMO)
    monkeypatch.setenv("MB_TEST_AUDIO", _tone(tmp_path, "m4a", 5))
    monkeypatch.setenv("MB_TEST_OUT_DIR", str(out))
    at = AppTest.from_function(_app_script, default_timeout=120)
    at.run()
    next(b for b in at.button if b.label == "▶ Run pipeline").click().run()
    assert not at.exception, at.exception
    errors = " ".join(str(e.value) for e in at.error)
    assert "413 TOO_LONG" in errors
    assert app.__dict__["_MB_STT_CALLS"] == []
    assert not any(str(s.value).startswith("Job `") for s in at.success)
