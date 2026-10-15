"""Regression: dates must never become blood pressure (OCR "Prescription Date").

Run:  python -m pytest demo/tests/test_bp_dates.py -q

Synthetic OCR text = the 9 EasyOCR lines of demo/ocr_test/test.png plus the
reported line "Prescription Date: 05/05/2023" (the committed test.png has no
date). Before: the AI digit gate (substring), the coords text fallback and the
judge (substring) all accepted "05" from the date -> BP 05/05, sys PASS.
Also: genuine BP (120/80, 130 by 80, 122 of 78, BPA mishear) still works, and
a frequency printed on the line after the drug is attached (marked to verify).
LLM output is faked; no Ollama, no patient data.
"""
import json
import os
import sys
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO = os.path.dirname(_HERE)
sys.path.insert(0, _DEMO)

import llm_extract  # noqa: E402
import ocr_llm_judge as J  # noqa: E402
from coords import resolve_slots  # noqa: E402
from stt_extract import run_text_extract  # noqa: E402

LINES = ["MEDICAL REPORT", "Patient: Test Patient", "Prescription Date: 05/05/2023", "Age: 45",
         "Chief Complaint:", "Fever for three days", "Temperature: 101 F", "Medication:",
         "Paracetamol 500 mg", "Twice daily"]
OCR = "\n".join(LINES)
LLM_DATE_AS_BP = {"patient_name": "Test Patient", "chief_complaint": "Fever", "drugs": [],
                  "vitals": {"sys": "05", "dia": "05", "temp": "", "spo2": ""},
                  "allergies_active": [], "allergies_denied": [], "symptoms_denied": []}


def _segs(lines):
    return [{"id": i, "text": t, "start": 0.0, "end": 0.0, "lang": "", "confidence": 0.9, "words": []}
            for i, t in enumerate(lines)]


def _image(lines):
    return run_text_extract("\n".join(lines), _segs(lines), "t", use_llm=False, source="image")


def _bp(ej, tj):
    s = {x["key"]: x["text"] for x in resolve_slots(ej, tj)["slots"]}
    return s["vitals_bp_sys"], s["vitals_bp_dia"]


@pytest.fixture
def fake_llm(monkeypatch):
    def install(extract_payload, judge_payload=None):
        monkeypatch.setattr(J, "ollama_available", lambda model="x": (True, ""))
        monkeypatch.setattr(llm_extract, "ollama_available", lambda model="x": (True, ""))
        judge_payload = judge_payload or {"fields": []}
        monkeypatch.setattr("subprocess.run", lambda args, **k: types.SimpleNamespace(
            stdout=json.dumps(judge_payload if "strict judge" in args[-1] else extract_payload), stderr=""))
    return install


# ---- dates are not BP ----

def test_regex_text_fallback_does_not_read_date_as_bp():
    lines = [ln for ln in LINES if not ln.startswith("Temperature")]  # no other vitals -> fallback runs
    tj, ej = _image(lines)
    assert _bp(ej, tj) == ("NIL", "NIL")


def test_regex_with_temperature_no_bp():
    tj, ej = _image(LINES)
    assert _bp(ej, tj) == ("NIL", "NIL")


def test_ai_extraction_rejects_date_digits_as_bp(fake_llm):
    fake_llm(LLM_DATE_AS_BP)
    ai = J.extract_llm_image(OCR, [{"line": i, "text": t} for i, t in enumerate(LINES)], _segs(LINES))
    assert "sys" not in ai["structured_vitals"] and "dia" not in ai["structured_vitals"]
    assert not any("05/05" in v["text"] for v in ai["vitals"])


def test_full_image_llm_path_bp_missing_not_pass(fake_llm):
    judge = {"fields": [{"field": "vitals_bp_sys", "status": "PASS", "line_no": 2, "confidence": 0.9},
                        {"field": "vitals_bp_dia", "status": "PASS", "line_no": 2, "confidence": 0.9}]}
    fake_llm(LLM_DATE_AS_BP, judge)
    tj, base = _image(LINES)
    merged, ev = J.run_image_llm_eval(base, OCR, _segs(LINES))
    assert _bp(merged, {**tj, "source": "image"}) == ("NIL", "NIL")
    by = {f["field"]: f for f in ev["fields"]}
    assert by["vitals_bp_sys"]["status"] == "MISSING" and by["vitals_bp_dia"]["status"] == "MISSING"


def test_judge_cannot_pass_bp_with_a_date_line(fake_llm):
    judge = {"fields": [{"field": "vitals_bp_sys", "status": "PASS", "line_no": 0, "confidence": 0.9},
                        {"field": "vitals_bp_dia", "status": "PASS", "line_no": 0, "confidence": 0.9}]}
    fake_llm({}, judge)
    lines = [{"line": 0, "text": "Prescription Date: 05/05/2023"}]
    ev = J.judge_fields([{"key": "vitals_bp_sys", "text": "05"}, {"key": "vitals_bp_dia", "text": "05"}],
                        "Prescription Date: 05/05/2023", lines)
    assert [f["status"] for f in ev["fields"]] == ["UNPROVEN", "UNPROVEN"]
    assert all(f["evidence_line"] == "" for f in ev["fields"])


def test_audio_ai_rejects_bp_without_bp_context(fake_llm):
    text = "Seen on 05/05/2023 with fever. Give paracetamol 500 mg."
    fake_llm(LLM_DATE_AS_BP)
    llm = llm_extract.extract_llm_primary(text, text, [])
    assert "sys" not in llm["structured_vitals"]


# ---- genuine BP still works ----

@pytest.mark.parametrize("text,expected", [("BP 120/80. Give paracetamol 500 mg.", ("120", "80")),
                                           ("BP 130 by 80, cough.", ("130", "80")),
                                           ("Blood pressure is sitting at 122 of 78.", ("122", "78")),
                                           ("BPA 130 by AC, give asitromaisin 500 mg once daily 3 days.", ("130", "80"))])
def test_genuine_spoken_bp_unchanged(text, expected):
    tj, ej = run_text_extract(text, [{"id": 0, "text": text, "start": 0.0, "end": 1.0}], "t", use_llm=False)
    assert _bp(ej, tj) == expected


def test_genuine_ocr_bp_with_date_present():
    lines = ["Prescription Date: 05/05/2023", "BP: 120/80", "Paracetamol 500 mg"]
    tj, ej = _image(lines)
    assert _bp(ej, tj) == ("120", "80")


def test_ai_genuine_bp_accepted_and_judge_passes(fake_llm):
    lines = ["Prescription Date: 05/05/2023", "B.P. 124 / 82 mmHg", "Paracetamol 500 mg"]
    ocr = "\n".join(lines)
    payload = dict(LLM_DATE_AS_BP, vitals={"sys": "124", "dia": "82", "temp": "", "spo2": ""})
    judge = {"fields": [{"field": "vitals_bp_sys", "status": "PASS", "line_no": 1, "confidence": 0.9},
                        {"field": "vitals_bp_dia", "status": "PASS", "line_no": 1, "confidence": 0.9}]}
    fake_llm(payload, judge)
    tj, base = _image(lines)
    merged, ev = J.run_image_llm_eval(base, ocr, _segs(lines))
    assert _bp(merged, {**tj, "source": "image"}) == ("124", "82")
    by = {f["field"]: f for f in ev["fields"]}
    assert by["vitals_bp_sys"]["status"] == "PASS" and by["vitals_bp_dia"]["status"] == "PASS"


# ---- medication fields ----

def test_frequency_on_next_ocr_line_attached_for_review():
    tj, ej = _image(LINES)
    s = {x["key"]: x for x in resolve_slots(ej, tj)["slots"]}
    assert (s["drug_0_name"]["text"], s["drug_0_dose"]["text"], s["drug_0_unit"]["text"]) == \
        ("paracetamol", "500", "mg")
    assert s["drug_0_freq"]["text"].lower() == "twice daily"
    assert s["drug_0_freq"]["color"] == "YELLOW" and "next" in s["drug_0_name"]["note"].lower()
    assert s["drug_0_duration"]["text"] == "NIL"  # truly not on the page


def test_next_line_that_is_not_a_frequency_is_not_attached():
    tj, ej = _image(["Paracetamol 500 mg", "Fever for three days"])
    assert ej["drugs"][0]["frequency"] == ""


def test_missing_note_does_not_claim_absent_from_ocr(fake_llm):
    fake_llm({}, {"fields": []})
    ev = J.judge_fields([{"key": "drug_0_duration", "text": "NIL"}], OCR,
                        [{"line": i, "text": t} for i, t in enumerate(LINES)])
    assert "not found in ocr" not in ev["fields"][0]["note"].lower()
