"""Regression: verification must follow the note content (stale-verify audit).

Run:  python -m pytest demo/tests/test_verification_state.py -q

Drives the real demo/app.py with Streamlit AppTest (fake uploader, fake
STT/OCR stages, synthetic note text; no patient data, no Whisper/Ollama).
app._HERE points at a temp dir, so audit files are written there only.
Covers: untouched Apply records nothing and cannot unlock Verify; a no-op
Apply or plain rerun keeps verification; any clinical edit (incl. reverting
to the AI value) after Verify requires re-verification; verified /
invalidated events stay in the audit trail; a new processing job (audio or
OCR) or a different upload never inherits verification.
"""
import io
import json
import math
import os
import struct
import sys
import wave

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO = os.path.dirname(_HERE)
sys.path.insert(0, _DEMO)

from streamlit.testing.v1 import AppTest  # noqa: E402

NOTE = ("Test cough. BP 120/80, temp 98.6 degrees, SpO2 98%. "
        "Give paracetamol 500 mg twice daily for 3 days. Diagnosis: test condition.")
OCR_LINES = ["Patient Name: Test Person", "Cough", "Tab Paracetamol 500 mg twice daily for 3 days",
             "BP 120/80", "Temp 98.6 degrees", "SpO2 98%", "Diagnosis: test condition"]


def _app_script():
    import json
    import os
    import shutil
    import sys

    sys.path.insert(0, os.environ["MB_TEST_DEMO_DIR"])
    import streamlit as st
    import app

    with open(os.environ["MB_TEST_CFG"], encoding="utf-8") as f:
        cfg = json.load(f)

    class _Up:
        def __init__(self, name, c):
            self.name, self._c = name, c

        def getvalue(self):
            return self._c

        def getbuffer(self):
            return memoryview(self._c)

    up = _Up(cfg["name"], bytes.fromhex(cfg["hex"]))

    def fake_clean(i, o, apply_denoise=True):
        shutil.copyfile(i, o)
        return {"duration_s": 0.5, "vad_ratio": 1.0, "denoise": "none"}

    def fake_stt(clean_wav, job_id="t", use_llm="auto", model="", ollama_model=""):
        from stt_extract import run_text_extract
        text = cfg["note"]
        tj, ej = run_text_extract(text, [{"id": 0, "text": text, "start": 0.0, "end": 2.0,
                                          "lang": "en-IN", "confidence": 0.9, "words": []}],
                                  job_id, use_llm=False, engine="fake-test")
        if cfg.get("icd"):
            ej["diagnosis"]["icd10"] = cfg["icd"]
        return tj, ej

    def fake_ocr(path):
        lines = [{"line": i, "text": t, "min_confidence": 0.95, "block_count": 1}
                 for i, t in enumerate(cfg["ocr_lines"])]
        return {"success": True, "text": "\n".join(cfg["ocr_lines"]), "engine": "fake-ocr",
                "source_file": path, "blocks": [], "lines": lines}

    saved = (st.file_uploader, app.clean_audio, app.run_stt_extract, app._ollama_available,
             app._HERE, app.extract_text_from_image)
    st.file_uploader = lambda *a, **k: up
    app.clean_audio = fake_clean
    app.run_stt_extract = fake_stt
    app._ollama_available = lambda model: (False, "disabled in test")
    app._HERE = os.environ["MB_TEST_OUT_DIR"]
    app.extract_text_from_image = fake_ocr
    try:
        app.main()
    finally:
        (st.file_uploader, app.clean_audio, app.run_stt_extract, app._ollama_available,
         app._HERE, app.extract_text_from_image) = saved


def _tone(freq):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / 16000)))
                               for i in range(4000)))
    return buf.getvalue()


@pytest.fixture
def env(tmp_path, monkeypatch):
    out = tmp_path / "out"
    for d in ("entities", "exports", "transcripts"):
        (out / d).mkdir(parents=True)
    cfg_path = tmp_path / "cfg.json"
    monkeypatch.setenv("MB_TEST_DEMO_DIR", _DEMO)
    monkeypatch.setenv("MB_TEST_CFG", str(cfg_path))
    monkeypatch.setenv("MB_TEST_OUT_DIR", str(out))

    def configure(name="synthetic_note_a.wav", freq=440, note=NOTE, icd="Z00.0", ocr_lines=OCR_LINES):
        cfg_path.write_text(json.dumps({"name": name, "hex": _tone(freq).hex(), "note": note,
                                        "icd": icd, "ocr_lines": ocr_lines}), encoding="utf-8")

    configure()
    return configure, out


def _start(image=False):
    at = AppTest.from_function(_app_script, default_timeout=120)
    at.run()
    if image:
        at.sidebar.radio[0].set_value("Image").run()
    next(c for c in at.checkbox if c.label.startswith("LLM")).set_value(False)
    return at


def _click(at, label):
    next(b for b in at.button if b.label == label).click().run()
    assert not at.exception, at.exception


def _job(at):
    for s in at.success:
        if str(s.value).startswith("Job `"):
            return str(s.value).split("`")[1]
    return None


def _apply(at, job, **edits):
    for sk, v in edits.items():
        at.text_input(key=f"{job}_{sk}").set_value(v)
    _click(at, "Apply corrections")


def _verify(at, job):
    btn = next(b for b in at.button if b.key == f"verify_{job}")
    assert not btn.disabled, "Verify unexpectedly locked"
    btn.click().run()
    assert not at.exception, at.exception


def _verified(at):
    return any("Verified — corrected exports unlocked" in str(s.value) for s in at.success)


def _needs_reverify(at):
    return any("re-verif" in str(w.value).lower() for w in at.warning)


def _trail(out, job):
    p = out / "entities" / f"{job}.audit.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def _corrections(trail):
    return [e for e in trail if e.get("slot_key")]


def _audit_caption(at):
    return next(str(c.value) for c in at.caption if str(c.value).startswith("Audited corrections"))


# ---- audio upload ----

def test_untouched_apply_records_no_corrections(env):
    _, out = env
    at = _start()
    _click(at, "▶ Run pipeline")
    job = _job(at)
    _click(at, "Apply corrections")  # nothing changed
    assert _corrections(_trail(out, job)) == []
    assert _audit_caption(at).startswith("Audited corrections: 0")
    assert next(b for b in at.button if b.key == f"verify_{job}").disabled  # gate not bypassed


def test_noop_apply_and_rerun_keep_verification(env):
    _, out = env
    at = _start()
    _click(at, "▶ Run pipeline")
    job = _job(at)
    _apply(at, job, drug_0_dose="650")
    _verify(at, job)
    assert _verified(at)
    at.run()  # harmless rerun
    assert _verified(at)
    _click(at, "Apply corrections")  # re-submit without changes
    assert _verified(at) and not _needs_reverify(at)
    assert len(_corrections(_trail(out, job))) == 1  # no duplicate entries


def test_clinical_edit_after_verify_requires_reverification(env):
    _, out = env
    at = _start()
    _click(at, "▶ Run pipeline")
    job = _job(at)
    _apply(at, job, drug_0_dose="650")
    _verify(at, job)
    _apply(at, job, vitals_bp_sys="135")
    assert not _verified(at)
    assert _needs_reverify(at)
    assert not next(b for b in at.button if b.key == f"verify_{job}").disabled  # can re-verify
    events = [e.get("event") for e in _trail(out, job)]
    assert "verified" in events and "verification_invalidated" in events  # history kept
    assert [e["slot_key"] for e in _corrections(_trail(out, job))] == ["drug_0_dose", "vitals_bp_sys"]
    _verify(at, job)
    assert _verified(at) and not _needs_reverify(at)
    assert [e.get("event") for e in _trail(out, job)].count("verified") == 2


def test_revert_to_ai_value_after_verify_requires_reverification(env):
    _, out = env
    at = _start()
    _click(at, "▶ Run pipeline")
    job = _job(at)
    _apply(at, job, drug_0_dose="650")
    _verify(at, job)
    _apply(at, job, drug_0_dose="500")  # back to the AI value: content still changed
    assert not _verified(at) and _needs_reverify(at)
    last = _corrections(_trail(out, job))[-1]
    assert last["slot_key"] == "drug_0_dose" and last["human_corrected"] == "500"


def test_new_processing_job_does_not_inherit_verification(env):
    _, out = env
    at = _start()
    _click(at, "▶ Run pipeline")
    job = _job(at)
    _apply(at, job, drug_0_dose="650")
    _verify(at, job)
    _click(at, "▶ Run pipeline")  # same file, processed again
    assert _job(at) == job and not _verified(at)
    assert at.text_input(key=f"{job}_drug_0_dose").value == "500"  # fresh AI value, not old edit


def test_switching_upload_does_not_inherit_verification(env):
    configure, _ = env
    at = _start()
    _click(at, "▶ Run pipeline")
    job_a = _job(at)
    _apply(at, job_a, drug_0_dose="650")
    _verify(at, job_a)
    configure(name="synthetic_note_b.wav", freq=880)
    at.run()
    assert _job(at) is None and not _verified(at)
    _click(at, "▶ Run pipeline")
    assert _job(at) != job_a and not _verified(at)


# ---- image (OCR) ----

def test_ocr_rerun_does_not_keep_stale_verification(env):
    configure, _ = env
    at = _start(image=True)
    _click(at, "▶ Run OCR")
    job = _job(at)
    _apply(at, job, dx_code_box="Z00.0")
    _verify(at, job)
    assert _verified(at)
    new = list(OCR_LINES)
    new[2] = "Tab Paracetamol 650 mg twice daily for 3 days"
    configure(ocr_lines=new)
    _click(at, "▶ Run OCR")  # new processing job for the same image
    assert _job(at) == job
    assert not _verified(at)
    assert at.text_input(key=f"{job}_drug_0_dose").value == "650"  # editor shows the new result
