"""LLM judge + AI eval for image OCR (ocr_llm_judge). Ollama is mocked.

Run:  python -m pytest demo/tests/test_ocr_llm_judge.py -q
Covers: gap-fill of regex-missed fields, invented values dropped (name +
digit gates), judge PASS/MISSING/UNPROVEN, fallback to regex-only.
"""
import json
import os
import sys
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO = os.path.dirname(_HERE)
sys.path.insert(0, _DEMO)

import ocr_llm_judge
from ocr_llm_judge import (
    _clean_ollama_output,
    extract_llm_image,
    judge_fields,
    merge_image,
    run_image_llm_eval,
)
from stt_extract import extract_entities, normalize_text, run_text_extract

OCR_TEXT = "\n".join([
    "Patient Name: Ravi Kumar",                                  # 0
    "Temp: 101 F",                                               # 1
    "Tab Paracetamol 500 mg twice daily for 3 days",             # 2
    "BP 130/80",                                                 # 3
    "No known allergy",                                          # 4
    "Impression: viral fever",                                   # 5
    "Review after 3 days",                                       # 6
])
LINES = [{"line": i, "text": t} for i, t in enumerate(OCR_TEXT.split("\n"))]


def _segs():
    return [{"id": i, "text": t, "start": 0.0, "end": 0.0,
             "lang": "", "confidence": 0.9, "words": []}
            for i, t in enumerate(OCR_TEXT.split("\n"))]


def _regex_base():
    norm = normalize_text(OCR_TEXT, source="image")
    return extract_entities(OCR_TEXT, norm["normalized_en"], _segs(), source="image")


EXTRACT_OK = {
    "patient_name": "Ravi Kumar",
    "chief_complaint": "fever",
    "drugs": [{"name": "paracetamol", "dose": 500, "unit": "mg",
               "frequency": "twice daily", "duration": "3 days",
               "source_line": "Tab Paracetamol 500 mg twice daily for 3 days",
               "line_no": 2}],
    "vitals": {"sys": "130", "dia": "80", "temp": "101 F", "spo2": ""},
    "vitals_proof": {"sys": "BP 130/80", "dia": "BP 130/80",
                     "temp": "Temp: 101 F", "spo2": ""},
    "allergies_active": [],
    "allergies_denied": [],
    "symptoms_denied": [],
    "diagnosis": {"text": "viral fever",
                  "source_line": "Impression: viral fever", "line_no": 5},
    "followup": {"text": "Review after 3 days",
                 "source_line": "Review after 3 days", "line_no": 6},
}

JUDGE_OK = {"fields": [
    {"field": "patient_name", "status": "PASS",
     "evidence_line": "Patient Name: Ravi Kumar", "line_no": 0, "confidence": 0.9},
    {"field": "cc_blank", "status": "PASS",
     "evidence_line": "Impression: viral fever", "line_no": 5, "confidence": 0.9},
    {"field": "drug_0_name", "status": "PASS",
     "evidence_line": "Tab Paracetamol 500 mg twice daily for 3 days",
     "line_no": 2, "confidence": 0.9},
    {"field": "drug_0_dose", "status": "PASS",
     "evidence_line": "Tab Paracetamol 500 mg twice daily for 3 days",
     "line_no": 2, "confidence": 0.9},
    {"field": "drug_0_unit", "status": "PASS",
     "evidence_line": "Tab Paracetamol 500 mg twice daily for 3 days",
     "line_no": 2, "confidence": 0.9},
    {"field": "drug_0_freq", "status": "PASS",
     "evidence_line": "Tab Paracetamol 500 mg twice daily for 3 days",
     "line_no": 2, "confidence": 0.9},
    {"field": "drug_0_duration", "status": "PASS",
     "evidence_line": "Tab Paracetamol 500 mg twice daily for 3 days",
     "line_no": 2, "confidence": 0.9},
    {"field": "vitals_bp_sys", "status": "PASS",
     "evidence_line": "BP 130/80", "line_no": 3, "confidence": 0.9},
    {"field": "vitals_bp_dia", "status": "PASS",
     "evidence_line": "BP 130/80", "line_no": 3, "confidence": 0.9},
    {"field": "vitals_temp", "status": "PASS",
     "evidence_line": "Temp: 101 F", "line_no": 1, "confidence": 0.9},
    {"field": "dx_text", "status": "PASS",
     "evidence_line": "Impression: viral fever", "line_no": 5, "confidence": 0.85},
    {"field": "fu_text", "status": "PASS",
     "evidence_line": "Review after 3 days", "line_no": 6, "confidence": 0.88},
]}


def _resp(payload):
    return types.SimpleNamespace(stdout=json.dumps(payload), stderr="")


def _install_fakes(monkeypatch, extract_payload=EXTRACT_OK, judge_payload=JUDGE_OK):
    monkeypatch.setattr(ocr_llm_judge, "ollama_available",
                        lambda model="llama3.2:3b": (True, ""))
    import llm_extract
    monkeypatch.setattr(llm_extract, "ollama_available",
                        lambda model="llama3.2:3b": (True, ""))

    def fake_run(cmd, **kw):
        prompt = cmd[-1] if isinstance(cmd, list) else ""
        if "strict judge" in prompt:
            return _resp(judge_payload)
        return _resp(extract_payload)

    monkeypatch.setattr("subprocess.run", fake_run)


def test_gapfill_diagnosis_and_judge_summary(monkeypatch):
    assert _regex_base().get("diagnosis", {}) == {}  # regex misses unlabeled dx
    _install_fakes(monkeypatch)
    merged, ev = run_image_llm_eval(_regex_base(), OCR_TEXT, _segs())
    assert merged["diagnosis"]["text"] == "viral fever"  # LLM gap-filled
    assert merged["llm_engine"].startswith("regex+llm-image:")
    assert ev["engine"].startswith("ai-eval:")
    assert ev["summary"] == {"pass": 12, "missing": 2, "unproven": 0}
    by_field = {f["field"]: f for f in ev["fields"]}
    assert by_field["dx_text"]["status"] == "PASS"
    assert by_field["allergy_line"]["status"] == "MISSING"  # truly absent
    assert by_field["vitals_spo2"]["status"] == "MISSING"
    assert "drug_1_name" not in by_field  # no 2nd drug -> not judged


def test_invented_drug_and_bp_dropped(monkeypatch):
    bad = dict(EXTRACT_OK)
    bad["drugs"] = [{"name": "mysterycure", "dose": 100, "unit": "mg",
                     "frequency": "daily", "duration": "5 days",
                     "source_line": "Take mysterycure 100 mg daily", "line_no": 9}]
    bad["vitals"] = {"sys": "999", "dia": "99", "temp": "", "spo2": ""}
    _install_fakes(monkeypatch, extract_payload=bad)
    merged, _ = run_image_llm_eval(_regex_base(), OCR_TEXT, _segs())
    assert [d["name"] for d in merged["drugs"]] == ["paracetamol"]
    assert "999" not in json.dumps(merged.get("vitals", []))
    assert merged.get("structured_vitals", {}).get("sys") != "999"


def test_judge_unproven_on_bad_evidence(monkeypatch):
    judge = {"fields": [
        {"field": "cc_blank", "status": "PASS",
         "evidence_line": "BP 999/99", "line_no": 3, "confidence": 0.9},
        {"field": "vitals_spo2", "status": "PASS",
         "evidence_line": "Temp: 101 F", "line_no": 1, "confidence": 0.9},
    ]}
    _install_fakes(monkeypatch, judge_payload=judge)
    merged, ev = run_image_llm_eval(_regex_base(), OCR_TEXT, _segs())
    by_field = {f["field"]: f for f in ev["fields"]}
    assert by_field["cc_blank"]["status"] == "UNPROVEN"  # evidence not verbatim
    assert by_field["vitals_spo2"]["status"] == "MISSING"  # NIL forced locally


def test_no_json_raises(monkeypatch):
    _install_fakes(monkeypatch)
    monkeypatch.setattr("subprocess.run",
                        lambda *a, **k: types.SimpleNamespace(stdout="hello", stderr=""))
    with pytest.raises(ValueError):
        extract_llm_image(OCR_TEXT, LINES, _segs())


def test_run_text_extract_image_fallback(monkeypatch):
    import llm_extract
    monkeypatch.setattr(llm_extract, "ollama_available",
                        lambda model="llama3.2:3b": (True, ""))

    def boom(*a, **k):
        raise ValueError("judge down")

    monkeypatch.setattr(ocr_llm_judge, "run_image_llm_eval", boom)
    tj, ej = run_text_extract(OCR_TEXT, _segs(), job_id="img-t",
                              use_llm=True, engine="easyocr", source="image")
    assert tj["source"] == "image"
    assert "regex-only (image source" in ej["llm_engine"]
    assert "ai_eval" not in ej
    assert ej["drugs"][0]["name"] == "paracetamol"  # regex floor intact


def test_run_text_extract_image_llm_path(monkeypatch):
    _install_fakes(monkeypatch)
    tj, ej = run_text_extract(OCR_TEXT, _segs(), job_id="img-t",
                              use_llm=True, engine="easyocr", source="image")
    assert ej["llm_engine"].startswith("regex+llm-image:")
    assert ej["ai_eval"]["summary"]["missing"] == 2
    assert ej["diagnosis"]["text"] == "viral fever"


def test_run_text_extract_image_regex_only_default(monkeypatch):
    tj, ej = run_text_extract(OCR_TEXT, _segs(), job_id="img-t",
                              use_llm=False, engine="easyocr", source="image")
    assert ej["llm_engine"] == "regex-only (image source)"
    assert "ai_eval" not in ej


def test_clean_ollama_output_joins_streaming_redraws():
    # Recorded shape of `ollama run` piped stdout: chunk + ESC[nD ESC[K +
    # newline + retyped chunk. Newlines after a clear are chunk joins.
    raw = ('{"a": "x", "drugs": [\x1b[1D\x1b[K\n'
           '[{"name": "Paracetamol", "dose": 500\x1b[3D\x1b[K\n'
           '500, "unit": "mg"}]}\n')
    clean = _clean_ollama_output(raw)
    assert "\x1b" not in clean
    assert json.loads(clean) == {"a": "x", "drugs": [
        {"name": "Paracetamol", "dose": 500, "unit": "mg"}]}
