"""Regression: different vital readings must not collapse silently (Bug #6).

Run:  python -m pytest demo/tests/test_vitals_multiple_readings.py -q

When a transcript/OCR page has more than one DIFFERENT reading of the same
vital, the compact A4 box can hold one value, so it must carry a
"multiple readings - verify" warning, list every reading (with its source
sentence) and never pick a "preferred" one. Identical repeated readings are
not a conflict. Synthetic inputs only; no Ollama, no patient data.
"""
import io
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

from coords import resolve_slots  # noqa: E402
from llm_extract import merge_primary  # noqa: E402
from stt_extract import run_text_extract  # noqa: E402

BP2 = "BP 150/95 on arrival. BP 130/80 after rest."


def _run(text, source="audio"):
    segs = [{"id": i, "text": t, "start": 0.0, "end": 0.0, "lang": "", "confidence": 0.9,
             "words": []} for i, t in enumerate(text.split("\n"))]
    return run_text_extract(text, segs, "t", use_llm=False, source=source)


def _resolve(ej, tj):
    r = resolve_slots(ej, tj, "er_discharge")
    return r, {s["key"]: s for s in r["slots"]}


def _assert_multi(r, slots, kind, keys, values):
    rs = r["multiple_vital_readings"][kind]
    assert [x["value"] for x in rs] == values
    assert all(x["sources"] and x["sources"][0] for x in rs)  # context kept
    for k in keys:
        note = slots[k]["note"].lower()
        assert "multiple readings" in note and "verify" in note
        assert all(v in slots[k]["note"] for v in values)
        assert slots[k]["color"] == "YELLOW"  # Verify behaviour unchanged (not RED)
    proof = " ".join(r["proof_lines"])
    assert all(v in proof for v in values)


def test_two_different_bp_readings_warn_and_keep_both():
    tj, ej = _run(BP2)
    assert [v["text"] for v in ej["vitals"]] == ["BP 150/95", "BP 130/80"]
    r, slots = _resolve(ej, tj)
    _assert_multi(r, slots, "bp", ["vitals_bp_sys", "vitals_bp_dia"], ["150/95", "130/80"])
    rs = r["multiple_vital_readings"]["bp"]
    assert "on arrival" in rs[0]["sources"][0] and "after rest" in rs[1]["sources"][0]


def test_two_different_temps_warn():
    tj, ej = _run("Temp 101 degrees at triage. Temp 99.5 degrees after rest.")
    r, slots = _resolve(ej, tj)
    _assert_multi(r, slots, "temp", ["vitals_temp"], ["101 degrees", "99.5 degrees"])


def test_celsius_and_fahrenheit_not_silently_equated():
    tj, ej = _run("Temp 38 degrees Celsius at home. Temp 100.4 degrees Fahrenheit here.")
    r, _ = _resolve(ej, tj)
    assert len(r["multiple_vital_readings"]["temp"]) == 2  # no unit conversion / choice


def test_two_different_spo2_warn():
    tj, ej = _run("SpO2 92% on arrival. SpO2 97% on oxygen.")
    r, slots = _resolve(ej, tj)
    _assert_multi(r, slots, "spo2", ["vitals_spo2"], ["92%", "97%"])


def test_identical_repeated_reading_is_not_a_conflict():
    tj, ej = _run("BP 130/80 on arrival. BP 130/80 after rest.")
    assert len(ej["vitals"]) == 2  # both mentions kept as extracted facts
    r, slots = _resolve(ej, tj)
    assert "bp" not in r["multiple_vital_readings"]
    assert slots["vitals_bp_sys"]["note"] == ""
    assert slots["vitals_bp_sys"]["text"] == "130"


def test_human_correction_resolves_warning_for_that_vital_only():
    tj, ej = _run(BP2 + " SpO2 92% on arrival. SpO2 97% on oxygen.")
    ej = {**ej, "structured_vitals": {"sys": "150", "dia": "95"}}  # doctor chose BP
    r, slots = _resolve(ej, tj)
    assert "bp" not in r["multiple_vital_readings"]
    assert (slots["vitals_bp_sys"]["text"], slots["vitals_bp_dia"]["text"]) == ("150", "95")
    assert slots["vitals_bp_sys"]["note"] == ""
    assert "spo2" in r["multiple_vital_readings"]  # untouched vital still flagged


def test_ocr_two_bp_lines_warn():
    tj, ej = _run("Name: Test Person\nBP 150/95\nBP 130/80", source="image")
    r, slots = _resolve(ej, tj)
    _assert_multi(r, slots, "bp", ["vitals_bp_sys", "vitals_bp_dia"], ["150/95", "130/80"])


def test_ai_reading_matching_one_regex_reading_is_not_a_conflict():
    tj, ej = _run(BP2)
    merged = merge_primary(ej, {"drugs": [], "vitals": [{"text": "BP 150/95"}],
                                "structured_vitals": {"sys": "150", "dia": "95"}})
    assert not merged.get("vitals_conflicts")  # AI agrees with one of the readings
    assert "sys" not in merged.get("structured_vitals", {})  # AI does not choose
    r, slots = _resolve(merged, tj)
    _assert_multi(r, slots, "bp", ["vitals_bp_sys"], ["150/95", "130/80"])


def test_ai_reading_matching_no_regex_reading_is_a_conflict():
    tj, ej = _run(BP2)
    merged = merge_primary(ej, {"drugs": [], "vitals": [],
                                "structured_vitals": {"sys": "140", "dia": "90"}})
    conf = {c["field"]: c for c in merged["vitals_conflicts"]}
    assert "150" in conf["sys"]["regex"] and "130" in conf["sys"]["regex"]
    assert conf["sys"]["ai"] == "140"


# ---- app: other readings accessible next to the editable form ----

def _app_script():
    import os
    import shutil
    import sys

    sys.path.insert(0, os.environ["MB_TEST_DEMO_DIR"])
    import streamlit as st
    import app

    class _Up:
        name = "synthetic_multi_bp.wav"

        def __init__(self, c):
            self._c = c

        def getvalue(self):
            return self._c

        def getbuffer(self):
            return memoryview(self._c)

    with open(os.environ["MB_TEST_WAV"], "rb") as f:
        up = _Up(f.read())

    def fake_clean(i, o, apply_denoise=True):
        shutil.copyfile(i, o)
        return {"duration_s": 1.0, "vad_ratio": 1.0, "snr_before_db": 40.0,
                "snr_after_db": 46.0, "denoise": "none"}

    def fake_stt(clean_wav, job_id="t", use_llm="auto", model="", ollama_model=""):
        from stt_extract import run_text_extract
        text = "BP 150/95 on arrival. BP 130/80 after rest."
        return run_text_extract(text, [{"id": 0, "text": text, "start": 0.0, "end": 2.0,
                                        "lang": "en-IN", "confidence": 0.9, "words": []}],
                                job_id, use_llm=False, engine="fake-test")

    saved = (st.file_uploader, app.clean_audio, app.run_stt_extract, app._ollama_available, app._HERE)
    st.file_uploader = lambda *a, **k: up
    app.clean_audio = fake_clean
    app.run_stt_extract = fake_stt
    app._ollama_available = lambda model: (False, "disabled in test")
    app._HERE = os.environ["MB_TEST_OUT_DIR"]
    try:
        app.main()
    finally:
        (st.file_uploader, app.clean_audio, app.run_stt_extract, app._ollama_available,
         app._HERE) = saved


def test_app_lists_all_readings_in_discharge_tab(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 330 * i / 16000)))
                               for i in range(8000)))
    wav = tmp_path / "tone.wav"
    wav.write_bytes(buf.getvalue())
    out = tmp_path / "out"
    for d in ("entities", "exports", "transcripts"):
        (out / d).mkdir(parents=True)
    monkeypatch.setenv("MB_TEST_DEMO_DIR", _DEMO)
    monkeypatch.setenv("MB_TEST_WAV", str(wav))
    monkeypatch.setenv("MB_TEST_OUT_DIR", str(out))
    at = AppTest.from_function(_app_script, default_timeout=120)
    at.run()
    next(b for b in at.button if b.label == "▶ Run pipeline").click().run()
    assert not at.exception, at.exception
    warn = " ".join(str(w.value) for w in at.main.warning)
    assert re.search(r"multiple .*BP.* readings", warn, re.I)
    assert "150/95" in warn and "130/80" in warn
    assert "on arrival" in warn and "after rest" in warn
    assert "not chosen automatically" in warn.lower() or "no reading was chosen" in warn.lower()
