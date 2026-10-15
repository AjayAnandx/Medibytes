"""Regression: uploaded-audio results survive Streamlit reruns (Bug #1).

Run:  python -m pytest demo/tests/test_app_upload_persistence.py -q

Drives the real demo/app.py with Streamlit AppTest. AppTest 1.40 has no
file_uploader support, so the test script swaps in a fake uploader plus fake
clean/STT stages (synthetic tone + synthetic entities, no patient data, no
Whisper/Ollama). app._HERE is pointed at a temp dir so nothing is written
into the repository.
Covers: result stays visible after Apply corrections and after Verify; the
pipeline is not re-run on those reruns; a different upload (even with the
same 12-char filename prefix) never reuses the previous result.
"""
import io
import json
import math
import os
import re
import struct
import sys
import wave

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO = os.path.dirname(_HERE)
sys.path.insert(0, _DEMO)

from streamlit.testing.v1 import AppTest  # noqa: E402


def _app_script():
    """Runs inside AppTest: fake upload + fake pipeline stages, then real app.main()."""
    import json
    import os
    import shutil
    import sys

    sys.path.insert(0, os.environ["MB_TEST_DEMO_DIR"])
    import streamlit as st
    import app

    with open(os.environ["MB_TEST_UPLOAD_CFG"], encoding="utf-8") as f:
        cfg = json.load(f)

    class _FakeUpload:
        def __init__(self, name, content):
            self.name = name
            self._content = content

        def getvalue(self):
            return self._content

        def getbuffer(self):
            return memoryview(self._content)

    up = _FakeUpload(cfg["name"], bytes.fromhex(cfg["hex"])) if cfg.get("name") else None
    calls = app.__dict__.setdefault("_MB_TEST_CALLS", [])

    def fake_clean(in_path, out_path, apply_denoise=True):
        shutil.copyfile(in_path, out_path)
        return {"duration_s": 0.5, "vad_ratio": 1.0, "snr_before_db": 40.0,
                "snr_after_db": 46.0, "denoise": "none"}

    def fake_stt(clean_wav, job_id="t", use_llm="auto", model="mock", ollama_model=""):
        calls.append(job_id)
        drug = cfg["drug"]
        text = f"Test cough. BP 120/80. Give {drug} 500 mg twice daily for 3 days."
        tj = {"job_id": job_id, "text": text, "language": "en-IN", "normalized_en": text,
              "normalizations": [], "stt_engine": "fake-test",
              "segments": [{"id": 0, "text": text, "start": 0.0, "end": 0.5,
                            "lang": "en-IN", "confidence": 0.9, "words": []}]}
        ej = {"job_id": job_id,
              "symptoms": [{"text": "cough", "confidence": 0.9, "color": "YELLOW",
                            "source_sentence": "Test cough.", "negated": False}],
              "drugs": [{"name": drug, "dose": 500.0, "unit": "mg", "frequency": "twice daily",
                         "duration": "3 days", "confidence": 0.96, "color": "GREEN",
                         "source_sentence": text, "negated": False}],
              "vitals": [],
              "structured_vitals": {"sys": "120", "dia": "80", "temp": "98.6 F", "spo2": "98%"},
              "allergies": [], "negations": [],
              "diagnosis": {"text": "test condition", "icd10": "Z00.0", "color": "YELLOW"},
              "followup": {}}
        return tj, ej

    saved = (st.file_uploader, app.clean_audio, app.run_stt_extract,
             app._ollama_available, app._HERE)
    st.file_uploader = lambda *a, **k: up
    app.clean_audio = fake_clean
    app.run_stt_extract = fake_stt
    app._ollama_available = lambda model: (False, "disabled in test")
    app._HERE = os.environ["MB_TEST_OUT_DIR"]
    try:
        app.main()
    finally:
        (st.file_uploader, app.clean_audio, app.run_stt_extract,
         app._ollama_available, app._HERE) = saved


def _tone_wav(freq):
    """0.5 s synthetic 16 kHz mono sine (distinct bytes per freq)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / 16000)))
                               for i in range(8000)))
    return buf.getvalue()


@pytest.fixture
def env(tmp_path, monkeypatch):
    for d in ("entities", "exports", "transcripts"):
        (tmp_path / "out" / d).mkdir(parents=True)
    cfg = tmp_path / "upload.json"
    monkeypatch.setenv("MB_TEST_DEMO_DIR", _DEMO)
    monkeypatch.setenv("MB_TEST_UPLOAD_CFG", str(cfg))
    monkeypatch.setenv("MB_TEST_OUT_DIR", str(tmp_path / "out"))
    import app
    app.__dict__["_MB_TEST_CALLS"] = []

    def set_upload(name, freq, drug="paracetamol"):
        cfg.write_text(json.dumps({"name": name, "hex": _tone_wav(freq).hex(), "drug": drug}),
                       encoding="utf-8")

    set_upload("synthetic_test_tone_a.wav", 440)
    return set_upload, app.__dict__["_MB_TEST_CALLS"]


def _new_app():
    at = AppTest.from_function(_app_script, default_timeout=120)
    at.run()
    assert not at.exception, at.exception
    return at


def _button(at, label):
    return next(b for b in at.button if b.label == label)


def _shown_job(at):
    """job id from the result banner, or None when no result is displayed."""
    for s in at.success:
        m = re.match(r"Job `([^`]+)`", str(s.value))
        if m:
            return m.group(1)
    return None


def _run_pipeline(at):
    _button(at, "▶ Run pipeline").click().run()
    assert not at.exception, at.exception
    job = _shown_job(at)
    assert job, "pipeline result not shown after Run"
    return job


def test_result_survives_apply_and_verify(env):
    _, calls = env
    at = _new_app()
    assert _shown_job(at) is None
    job = _run_pipeline(at)
    assert len(calls) == 1

    # Apply corrections -> st.rerun(): result must still be on screen
    at.text_input(key=f"{job}_drug_0_dose").set_value("650")
    _button(at, "Apply corrections").click().run()
    assert not at.exception, at.exception
    assert _shown_job(at) == job, "uploaded result disappeared after Apply corrections"
    assert at.text_input(key=f"{job}_drug_0_dose").value == "650"
    assert len(calls) == 1, "pipeline must not re-run on Apply corrections"

    # Verify -> st.rerun(): result and verified state must persist
    verify = next(b for b in at.button if b.key == f"verify_{job}")
    assert not verify.disabled
    verify.click().run()
    assert not at.exception, at.exception
    assert _shown_job(at) == job, "uploaded result disappeared after Verify"
    assert any("Verified" in str(s.value) for s in at.success)
    assert len(calls) == 1, "pipeline must not re-run on Verify"

    # plain rerun (any other widget interaction) keeps it too
    at.run()
    assert _shown_job(at) == job


def test_different_upload_does_not_reuse_result(env):
    set_upload, calls = env
    at = _new_app()
    job_a = _run_pipeline(at)

    # same 12-char filename prefix, different content -> must not show file A's result
    set_upload("synthetic_test_tone_b.wav", 880, drug="ibuprofen")
    at.run()
    assert not at.exception, at.exception
    assert _shown_job(at) is None, "previous upload's result shown for a different file"
    assert any("Press ▶ Run pipeline" in str(i.value) for i in at.info)

    job_b = _run_pipeline(at)
    assert job_b != job_a
    assert at.text_input(key=f"{job_b}_drug_0_name").value == "ibuprofen"
    assert len(calls) == 2

    # back to file A without pressing Run: B's result must not be shown for A
    set_upload("synthetic_test_tone_a.wav", 440)
    at.run()
    assert _shown_job(at) != job_b
