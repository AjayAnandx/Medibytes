"""Regression: a Whisper failure must never become a fake transcript (Bug #3).

Run:  python -m pytest demo/tests/test_stt_failure.py -q

faster_whisper is replaced in sys.modules by fakes that fail (model load,
mid-transcription, not installed). Audio is a synthetic tone; no patient
data, no Whisper/Ollama. Covers stt_extract.transcribe/run_stt_extract, the
CLI (pipeline.main) and the Streamlit upload flow (AppTest), plus explicit
--model mock still working and being labelled synthetic.
"""
import io
import json
import math
import os
import struct
import sys
import types
import wave

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO = os.path.dirname(_HERE)
sys.path.insert(0, _DEMO)

import pipeline  # noqa: E402
import stt_extract  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

MOCK_PHRASES = ("paracetamol", "azithromycin", "bukhar", "Khansi")


def _tone_bytes(freq=440):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / 16000)))
                               for i in range(16000)))
    return buf.getvalue()


def _fake_whisper(fail_on="load"):
    """Fake faster_whisper module: fails at model load or mid-transcription."""
    mod = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, *a, **k):
            if fail_on == "load":
                raise RuntimeError("synthetic model load failure")

        def transcribe(self, *a, **k):
            def segs():
                raise RuntimeError("synthetic decode failure")
                yield  # pragma: no cover
            return segs(), types.SimpleNamespace(language="en")

    mod.WhisperModel = WhisperModel
    return mod


@pytest.fixture
def wav(tmp_path):
    p = tmp_path / "synthetic_tone.wav"
    p.write_bytes(_tone_bytes())
    return str(p)


def _assert_transcription_error(excinfo, *needles):
    assert type(excinfo.value).__name__ == "TranscriptionError"
    msg = str(excinfo.value)
    for n in needles:
        assert n in msg
    assert not any(p.lower() in msg.lower() for p in MOCK_PHRASES)


@pytest.mark.parametrize("job_id", ["live-synthetic-1a2b3c4d", "demo-001", "sample1"])
def test_model_load_failure_raises_not_mock(monkeypatch, wav, job_id):
    # demo-001 / sample1 are MOCK_TEXTS keys: previously they got that script
    monkeypatch.setitem(sys.modules, "faster_whisper", _fake_whisper("load"))
    with pytest.raises(Exception) as ei:
        stt_extract.transcribe(wav, job_id, model="base-int8")
    _assert_transcription_error(ei, "synthetic model load failure")


def test_failure_during_decoding_raises(monkeypatch, wav):
    monkeypatch.setitem(sys.modules, "faster_whisper", _fake_whisper("decode"))
    with pytest.raises(Exception) as ei:
        stt_extract.transcribe(wav, "live-x", model="tiny-int8")
    _assert_transcription_error(ei, "synthetic decode failure")


def test_whisper_not_installed_raises_with_hint(monkeypatch, wav):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # -> ImportError
    with pytest.raises(Exception) as ei:
        stt_extract.transcribe(wav, "live-x", model="base-int8")
    _assert_transcription_error(ei, "faster-whisper")


def test_run_stt_extract_produces_no_entities_on_failure(monkeypatch, wav):
    monkeypatch.setitem(sys.modules, "faster_whisper", _fake_whisper("load"))
    with pytest.raises(Exception) as ei:
        stt_extract.run_stt_extract(wav, "live-x", use_llm=False, model="base-int8")
    assert type(ei.value).__name__ == "TranscriptionError"


def test_explicit_mock_still_works_and_is_labelled_mock(wav):
    # engine label unchanged (IPD ER-baseline hashes pin it); UI/CLI key off "mock"
    out = stt_extract.transcribe(wav, "demo-001", model="mock")
    assert out["text"] == stt_extract.MOCK_TEXTS["demo-001"]
    assert out["engine"].startswith("mock")


def test_pipeline_cli_labels_explicit_mock_as_synthetic(monkeypatch, tmp_path, wav, capsys):
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(pipeline, "_HERE", str(out))
    monkeypatch.setattr(sys, "argv", ["pipeline.py", "--in", wav, "--key", "demo-001",
                                      "--model", "mock", "--no-llm", "--template", "none"])
    assert pipeline.main() == 0
    assert "SYNTHETIC transcript" in capsys.readouterr().out


def test_pipeline_cli_fails_cleanly_and_writes_nothing(monkeypatch, tmp_path, wav, capsys):
    monkeypatch.setitem(sys.modules, "faster_whisper", _fake_whisper("load"))
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(pipeline, "_HERE", str(out))

    def fake_clean(in_path, out_path, apply_denoise=True):
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(in_path, "rb") as s, open(out_path, "wb") as d:
            d.write(s.read())
        return {"vad_ratio": 1.0, "snr_before_db": 40.0, "snr_after_db": 46.0}

    monkeypatch.setattr(pipeline, "clean_audio", fake_clean)
    monkeypatch.setattr(sys, "argv", ["pipeline.py", "--in", wav, "--key", "demo-001",
                                      "--model", "base-int8", "--no-llm"])
    rc = pipeline.main()
    err = capsys.readouterr().err
    assert rc not in (0, None)
    assert "transcription failed" in err.lower()
    for d in ("transcripts", "entities", "exports"):
        assert not (out / d).exists() or not os.listdir(out / d), f"{d} written after STT failure"


# ---- Streamlit upload flow ----

def _app_script():
    import os
    import shutil
    import sys
    import types

    sys.path.insert(0, os.environ["MB_TEST_DEMO_DIR"])
    import streamlit as st
    import app

    class _FakeUpload:
        name = "synthetic_test_tone.wav"

        def __init__(self, content):
            self._content = content

        def getvalue(self):
            return self._content

        def getbuffer(self):
            return memoryview(self._content)

    with open(os.environ["MB_TEST_WAV"], "rb") as f:
        up = _FakeUpload(f.read())

    fw = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, *a, **k):
            raise RuntimeError("synthetic model load failure")

    fw.WhisperModel = WhisperModel

    def fake_clean(in_path, out_path, apply_denoise=True):
        shutil.copyfile(in_path, out_path)
        return {"duration_s": 1.0, "vad_ratio": 1.0, "snr_before_db": 40.0,
                "snr_after_db": 46.0, "denoise": "none"}

    saved = (st.file_uploader, app.clean_audio, app._ollama_available, app._HERE,
             sys.modules.get("faster_whisper"))
    st.file_uploader = lambda *a, **k: up
    app.clean_audio = fake_clean
    app._ollama_available = lambda model: (False, "disabled in test")
    app._HERE = os.environ["MB_TEST_OUT_DIR"]
    sys.modules["faster_whisper"] = fw
    try:
        app.main()
    finally:
        (st.file_uploader, app.clean_audio, app._ollama_available, app._HERE,
         sys.modules["faster_whisper"]) = saved


@pytest.fixture
def app_env(tmp_path, monkeypatch, wav):
    out = tmp_path / "out"
    for d in ("entities", "exports", "transcripts"):
        (out / d).mkdir(parents=True)
    monkeypatch.setenv("MB_TEST_DEMO_DIR", _DEMO)
    monkeypatch.setenv("MB_TEST_WAV", wav)
    monkeypatch.setenv("MB_TEST_OUT_DIR", str(out))
    at = AppTest.from_function(_app_script, default_timeout=120)
    at.run()
    next(c for c in at.checkbox if c.label.startswith("LLM extraction")).set_value(False)
    return at, out


def _run(at):
    next(b for b in at.button if b.label == "▶ Run pipeline").click().run()
    assert not at.exception, at.exception


def _texts(at):
    """Main-area text only (the sidebar narration legitimately mentions bukhar)."""
    m = at.main
    return " ".join(str(x.value) for x in list(m.success) + list(m.markdown) + list(m.code)
                    + list(m.text_input) + list(m.text_area) + list(m.json))


def test_app_upload_whisper_failure_shows_error_not_fake_note(app_env):
    at, out = app_env
    _run(at)
    errors = " ".join(str(e.value) for e in at.error)
    assert "transcription failed" in errors.lower()
    assert "synthetic model load failure" in errors
    assert not any(str(s.value).startswith("Job `") for s in at.success), "result shown after failure"
    shown = _texts(at)
    assert not any(p.lower() in shown.lower() for p in MOCK_PHRASES), "fake medical text displayed"
    for d in ("transcripts", "entities", "exports"):
        assert not os.listdir(out / d), f"{d} persisted after failed transcription"


def test_app_explicit_mock_is_clearly_labelled_synthetic(app_env):
    at, _ = app_env
    next(s for s in at.selectbox if s.label == "STT model").set_value("mock (instant script)")
    _run(at)
    assert any(str(s.value).startswith("Job `") for s in at.success)
    warnings = " ".join(str(w.value) for w in at.warning).lower()
    assert "synthetic" in warnings and "not your audio" in warnings
