"""Regression: allergy information must reach the discharge note correctly.

Run:  python -m pytest demo/tests/test_allergies.py -q

Synthetic text only (no patient data, no Ollama: LLM output is faked).
Rules pinned here: documented allergies (one or many, audio or OCR, regex or
AI) appear in the note + HTML + DOCX; "no known allergies" is shown only when
explicitly documented; missing information is "NOT DOCUMENTED", never NKA;
conflicts are flagged RED for clinician review, not silently resolved; AI
allergens must be present in the source text; clinician edits win.
"""
import io
import json
import os
import sys
import types
from datetime import datetime, timezone

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO = os.path.dirname(_HERE)
sys.path.insert(0, _DEMO)

import llm_extract  # noqa: E402
from coords import resolve_slots  # noqa: E402
from fill_template import fill_template  # noqa: E402
from ocr_llm_judge import merge_image  # noqa: E402
from stt_extract import run_text_extract  # noqa: E402

MEDS = "Give paracetamol 500 mg twice daily for 3 days. BP 120/80."


def _run(text, source="audio"):
    segs = [{"id": i, "text": t, "start": 0.0, "end": 0.0, "lang": "", "confidence": 0.9,
             "words": []} for i, t in enumerate(text.split("\n"))]
    return run_text_extract(text, segs, "t", use_llm=False, source=source)


def _allergy_slot(ej, tj):
    return next(s for s in resolve_slots(ej, tj, "er_discharge")["slots"] if s["key"] == "allergy_line")


def _docx_text(b):
    from docx import Document
    return "\n".join(p.text for p in Document(io.BytesIO(b)).paragraphs)


def _active(ej):
    return sorted(a["text"].lower() for a in ej["allergies"] if not a.get("negated"))


def _denied(ej):
    return sorted(a["text"].lower() for a in ej["allergies"] if a.get("negated"))


# ---- documented allergies reach the note ----

def test_single_drug_allergy_in_note_html_docx():
    tj, ej = _run("Patient is allergic to penicillin. " + MEDS)
    assert _active(ej) == ["penicillin"]
    a = next(x for x in ej["allergies"] if x["text"].lower() == "penicillin")
    assert "allergic to penicillin" in a["source_sentence"].lower()  # source kept
    slot = _allergy_slot(ej, tj)
    assert "penicillin" in slot["text"].lower() and slot["color"] == "YELLOW"
    out = fill_template(ej, tj)
    assert "penicillin" in out["html"].lower()
    assert "penicillin" in _docx_text(out["docx_bytes"]).lower()
    # medications / vitals unchanged by allergy handling
    assert ej["drugs"][0]["name"] == "paracetamol" and ej["drugs"][0]["dose"] == 500.0
    assert [v["text"] for v in ej["vitals"]] == ["BP 120/80"]


def test_multiple_allergies_preserved_audio():
    tj, ej = _run("Allergic to penicillin and sulfa. " + MEDS)
    assert _active(ej) == ["penicillin", "sulfa"]
    text = _allergy_slot(ej, tj)["text"].lower()
    assert "penicillin" in text and "sulfa" in text


def test_multiple_allergies_preserved_ocr_label():
    tj, ej = _run("Name: Test Person\nAllergies: Penicillin, Ibuprofen\nBP 120/80", source="image")
    assert _active(ej) == ["ibuprofen", "penicillin"]
    out = fill_template(ej, tj)
    assert "ibuprofen" in out["html"].lower() and "ibuprofen" in _docx_text(out["docx_bytes"]).lower()


def test_active_and_denied_in_one_sentence_not_flipped():
    tj, ej = _run("Allergic to penicillin, denies sulfa allergy. " + MEDS)
    assert _active(ej) == ["penicillin"]  # was recorded as DENIED before
    assert "sulfa" in _denied(ej)
    assert "penicillin" not in _denied(ej)


# ---- no known allergies vs not documented ----

@pytest.mark.parametrize("text,source", [("No known drug allergies. " + MEDS, "audio"),
                                         ("NKDA. " + MEDS, "audio"),
                                         ("No allergy. " + MEDS, "audio"),
                                         ("Patient denies any allergies. " + MEDS, "audio"),
                                         ("Name: Test Person\nAllergies: None\nBP 120/80", "image")])
def test_explicit_nka_represented(text, source):
    tj, ej = _run(text, source)
    assert _active(ej) == []
    slot = _allergy_slot(ej, tj)
    assert slot["text"].lower().startswith("no known allergies")
    assert slot["color"] != "GREEN"  # extracted statement, clinician still verifies


@pytest.mark.parametrize("text", ["Test cough. " + MEDS, "No allergy to penicillin. " + MEDS])
def test_missing_status_is_not_documented_never_nka(text):
    tj, ej = _run(text)
    slot = _allergy_slot(ej, tj)
    assert "not documented" in slot["text"].lower()
    assert "no known" not in slot["text"].lower()
    html = fill_template(ej, tj)["html"].lower()
    assert "not documented" in html and "no known allergies" not in html


def test_drug_specific_denial_kept_as_denied():
    tj, ej = _run("No allergy to penicillin. " + MEDS)
    assert _denied(ej) == ["penicillin"]
    assert "penicillin" in [d["text"] for d in resolve_slots(ej, tj)["denied"]]


# ---- conflicts flagged, not resolved ----

def test_nka_plus_active_allergy_is_conflict():
    tj, ej = _run("No known allergies. Patient is allergic to penicillin. " + MEDS)
    slot = _allergy_slot(ej, tj)
    assert "penicillin" in slot["text"].lower()  # never hidden behind NKA
    assert slot["color"] == "RED" and "conflict" in slot["note"].lower()
    assert any("allergy conflict" in p.lower() for p in resolve_slots(ej, tj)["proof_lines"])


def test_same_allergen_active_and_denied_is_conflict():
    tj, ej = _run("Patient is allergic to penicillin. No penicillin allergy. " + MEDS)
    slot = _allergy_slot(ej, tj)
    assert slot["color"] == "RED" and "penicillin" in slot["note"].lower()


def test_unrelated_active_and_denied_no_conflict():
    tj, ej = _run("Allergic to penicillin, denies sulfa allergy. " + MEDS)
    slot = _allergy_slot(ej, tj)
    assert slot["color"] == "YELLOW" and "conflict" not in slot["note"].lower()


# ---- AI paths ----

def _fake_llm(monkeypatch, payload):
    monkeypatch.setattr(llm_extract, "ollama_available", lambda model="x": (True, ""))
    monkeypatch.setattr("subprocess.run",
                        lambda *a, **k: types.SimpleNamespace(stdout=json.dumps(payload), stderr=""))


def test_audio_llm_active_allergy_kept_and_invented_dropped(monkeypatch):
    text = "Penicillin gives this patient a rash. " + MEDS
    _fake_llm(monkeypatch, {"chief_complaint": "", "drugs": [], "vitals": {},
                            "allergies_active": ["penicillin", "latex"], "allergies_denied": []})
    llm = llm_extract.extract_llm_primary(text, text, [])
    tj, base = _run(text)
    merged = llm_extract.merge_primary(base, llm)
    assert _active(merged) == ["penicillin"]  # latex is not in the transcript -> dropped
    a = next(x for x in merged["allergies"] if x["text"] == "penicillin")
    assert a["color"] == "YELLOW" and "verify" in a.get("note", "")
    assert "penicillin" in _allergy_slot(merged, tj)["text"].lower()


def test_ocr_llm_active_allergy_reaches_note():
    ocr = "Name: Test Person\nRash with penicillin\nBP 120/80"
    tj, base = _run(ocr, source="image")
    merged = merge_image(base, {"drugs": [], "allergies": [
        {"text": "penicillin", "negated": False, "confidence": 0.82, "color": "YELLOW",
         "source_sentence": "", "note": "llm-image - verify"}]}, ocr)
    assert "penicillin" in _allergy_slot(merged, tj)["text"].lower()


def test_string_allergies_from_llm_tidy_do_not_crash_or_vanish():
    ej = {"allergies": ["penicillin"], "symptoms": [], "drugs": [], "vitals": []}
    slot = _allergy_slot(ej, {"text": "", "segments": []})
    assert "penicillin" in slot["text"].lower() and slot["color"] == "YELLOW"


# ---- clinician edits ----

def test_clinician_nka_entry_is_status_not_allergen():
    import app
    tj, ej = _run("Test cough. " + MEDS)
    defaults = {s["key"]: s["text"] for s in resolve_slots(ej, tj)["slots"]}
    patched, audit = app._apply_edits(ej, {"allergy_line": "No known allergies"}, defaults)
    assert _active(patched) == []
    assert _allergy_slot(patched, tj)["text"].lower().startswith("no known allergies")
    assert [a["slot_key"] for a in audit] == ["allergy_line"]


def test_clinician_edit_resolves_conflict():
    import app
    tj, ej = _run("No known allergies. Patient is allergic to penicillin. " + MEDS)
    defaults = {s["key"]: s["text"] for s in resolve_slots(ej, tj)["slots"]}
    patched, _ = app._apply_edits(ej, {"allergy_line": "penicillin"}, defaults)
    slot = _allergy_slot(patched, tj)
    assert slot["color"] == "YELLOW" and "conflict" not in slot["note"].lower()
    assert _active(patched) == ["penicillin"]


# ---- IPD receives the allergy as an event ----

def test_ipd_event_for_documented_allergy():
    from ipd.event_extractor import extract_events
    from ipd.models import EventCategory, Role, SourceType
    _, ej = _run("Patient is allergic to penicillin. " + MEDS, source="image")
    events = extract_events(ej, encounter_id="enc_test", source_capture_id="cap_test",
                            occurred_at=datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc),
                            author_id="u_test", author_role=Role.RESIDENT,
                            source_type=SourceType.TYPED, source_text="Patient is allergic to penicillin.")
    allergy = [e for e in events if e.category == EventCategory.ALLERGY]
    assert [(e.subtype, e.payload["substance"].lower()) for e in allergy] == [("active", "penicillin")]


# ---- OCR AI judge: allergy status evaluated, not invented ----

def _judge(value_text, verdict_line, ocr):
    import ocr_llm_judge
    lines = [{"line": i, "text": t} for i, t in enumerate(ocr.split("\n"))]
    payload = {"fields": [{"field": "allergy_line", "status": "PASS", "line_no": verdict_line,
                           "evidence_line": "", "confidence": 0.9}]}
    orig_avail, orig_run = ocr_llm_judge.ollama_available, ocr_llm_judge.subprocess.run
    ocr_llm_judge.ollama_available = lambda model="x": (True, "")
    ocr_llm_judge.subprocess.run = lambda *a, **k: types.SimpleNamespace(stdout=json.dumps(payload), stderr="")
    try:
        ev = ocr_llm_judge.judge_fields([{"key": "allergy_line", "text": value_text}], ocr, lines)
    finally:
        ocr_llm_judge.ollama_available, ocr_llm_judge.subprocess.run = orig_avail, orig_run
    return ev["fields"][0]


def test_judge_nka_needs_real_nka_line():
    from coords import ALLERGY_NKA_TEXT
    ocr = "Name: Test Person\nNo known drug allergies\nBP 120/80"
    assert _judge(ALLERGY_NKA_TEXT, 1, ocr)["status"] == "PASS"
    assert _judge(ALLERGY_NKA_TEXT, 2, ocr)["status"] == "UNPROVEN"  # BP line does not state NKA


def test_judge_not_documented_is_missing():
    from coords import ALLERGY_MISSING_TEXT
    assert _judge(ALLERGY_MISSING_TEXT, 1, "Name: Test Person\nBP 120/80")["status"] == "MISSING"
