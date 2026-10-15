"""Regression: AI vitals must not erase regex vitals (Bug #2).

Run:  python -m pytest demo/tests/test_vitals_merge.py -q

Synthetic inputs only (no patient data, no Ollama). Covers coords slot
resolution, the audio merge (llm_extract.merge_primary) and the image merge
(ocr_llm_judge.merge_image): non-conflicting regex + AI vitals both kept,
regex never overwritten, disagreements recorded (not silently resolved),
no duplicate unit-less AI vitals, units preserved, nothing invented, human
corrections still authoritative.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO = os.path.dirname(_HERE)
sys.path.insert(0, _DEMO)

from coords import resolve_slots  # noqa: E402
from llm_extract import merge_primary  # noqa: E402
from ocr_llm_judge import merge_image  # noqa: E402
from stt_extract import extract_entities, normalize_text  # noqa: E402


def _audio_base(text):
    norm = normalize_text(text)
    segs = [{"id": 0, "text": text, "start": 0.0, "end": 5.0, "lang": "en-IN",
             "confidence": 0.9, "words": []}]
    ent = extract_entities(text, norm["normalized_en"], segs)
    tj = {"job_id": "t", "text": text, "language": "en-IN", "segments": segs,
          "normalized_en": norm["normalized_en"]}
    return ent, tj


def _image_base(ocr_text):
    segs = [{"id": i, "text": t, "start": 0.0, "end": 0.0, "lang": "", "confidence": 0.9,
             "words": []} for i, t in enumerate(ocr_text.split("\n"))]
    norm = normalize_text(ocr_text, source="image")
    ent = extract_entities(ocr_text, norm["normalized_en"], segs, source="image")
    tj = {"job_id": "t", "text": ocr_text, "normalized_en": norm["normalized_en"],
          "segments": segs, "source": "image"}
    return ent, tj


def _vitals(ent, tj):
    slots = resolve_slots({"job_id": "t", **ent}, tj, "er_discharge")["slots"]
    return {s["key"]: s for s in slots if s["key"].startswith("vitals_")}


def _txt(v):
    return {k: s["text"] for k, s in v.items()}


# ---- coords: structured_vitals fills per field, never wipes regex boxes ----

def test_coords_regex_bp_kept_when_ai_adds_spo2():
    ent = {"vitals": [{"text": "BP 130/80", "confidence": 0.87, "color": "YELLOW",
                       "source_sentence": "BP 130/80."}],
           "structured_vitals": {"spo2": "99%"}}
    tj = {"text": "BP 130/80.", "normalized_en": "BP 130/80.", "segments": []}
    v = _vitals(ent, tj)
    assert _txt(v) == {"vitals_bp_sys": "130", "vitals_bp_dia": "80",
                       "vitals_temp": "NIL", "vitals_spo2": "99%"}
    assert v["vitals_bp_sys"]["color"] == "YELLOW"
    assert v["vitals_temp"]["color"] == "RED"  # missing stays missing, nothing invented


def test_coords_human_correction_still_authoritative():
    ent = {"vitals": [{"text": "BP 130/80"}, {"text": "SpO2 97%"}],
           "structured_vitals": {"sys": "135", "spo2": ""}}  # human: fix sys, clear spo2
    tj = {"text": "BP 130/80. SpO2 97%.", "normalized_en": "BP 130/80. SpO2 97%.", "segments": []}
    v = _vitals(ent, tj)
    assert v["vitals_bp_sys"]["text"] == "135"
    assert v["vitals_bp_dia"]["text"] == "80"  # untouched regex value kept
    assert v["vitals_spo2"]["text"] == "NIL" and v["vitals_spo2"]["color"] == "RED"


# ---- audio merge (merge_primary) ----

def test_audio_regex_bp_plus_ai_spo2_both_kept():
    base, tj = _audio_base("Test patient seen today. BP 130/80.")
    assert _txt(_vitals(base, tj))["vitals_bp_sys"] == "130"  # regex found BP
    llm = {"drugs": [], "vitals": [{"text": "99%", "confidence": 0.87, "color": "YELLOW",
                                    "source_sentence": ""}],
           "structured_vitals": {"spo2": "99%"}}
    merged = merge_primary(base, llm)
    v = _txt(_vitals(merged, tj))
    assert v == {"vitals_bp_sys": "130", "vitals_bp_dia": "80",
                 "vitals_temp": "NIL", "vitals_spo2": "99%"}


def test_audio_ai_never_overwrites_regex_and_conflict_is_recorded():
    base, tj = _audio_base("Test patient seen today. BP 130/80.")
    llm = {"drugs": [], "vitals": [{"text": "BP 140/90"}],
           "structured_vitals": {"sys": "140", "dia": "90"}}
    merged = merge_primary(base, llm)
    v = _vitals(merged, tj)
    assert (v["vitals_bp_sys"]["text"], v["vitals_bp_dia"]["text"]) == ("130", "80")
    assert "sys" not in merged.get("structured_vitals", {})
    conf = {c["field"]: c for c in merged.get("vitals_conflicts", [])}
    assert conf["sys"]["regex"] == "130" and conf["sys"]["ai"] == "140"
    assert conf["dia"]["regex"] == "80" and conf["dia"]["ai"] == "90"
    assert "140" in v["vitals_bp_sys"]["note"]  # surfaced on the note, not silent
    assert "140/90" not in [x["text"] for x in merged["vitals"]]


def test_audio_same_value_without_unit_is_not_duplicated():
    base, tj = _audio_base("Test patient seen today. Temp 100.4 degrees Fahrenheit, SpO2 99%.")
    n_before = len(base["vitals"])
    llm = {"drugs": [], "vitals": [{"text": "100.4"}, {"text": "99"}],
           "structured_vitals": {"temp": "100.4", "spo2": "99"}}
    merged = merge_primary(base, llm)
    assert len(merged["vitals"]) == n_before  # no "100.4" / "99" duplicates
    assert not merged.get("vitals_conflicts")  # same reading, just no unit
    v = _txt(_vitals(merged, tj))
    assert "100.4" in v["vitals_temp"] and "F" in v["vitals_temp"].upper()  # regex unit kept
    assert v["vitals_spo2"].startswith("99")


def test_audio_temp_unit_mismatch_is_a_conflict():
    base, _ = _audio_base("Test patient seen today. Temp 38 degrees Celsius.")
    merged = merge_primary(base, {"drugs": [], "vitals": [],
                                  "structured_vitals": {"temp": "38 F"}})
    assert [c["field"] for c in merged.get("vitals_conflicts", [])] == ["temp"]
    assert "temp" not in merged.get("structured_vitals", {})


def test_audio_ai_temp_unit_preserved_on_gapfill():
    base, tj = _audio_base("Test patient seen today. BP 120/70.")
    merged = merge_primary(base, {"drugs": [], "vitals": [{"text": "100.4 F"}],
                                  "structured_vitals": {"temp": "100.4 F"}})
    v = _txt(_vitals(merged, tj))
    assert v["vitals_temp"] == "100.4 F"
    assert (v["vitals_bp_sys"], v["vitals_bp_dia"]) == ("120", "70")


# ---- image merge (merge_image) ----

def test_image_regex_bp_plus_ai_spo2_both_kept():
    ocr = "Name: Test Person\nBP 130/80\nSaturation 99"
    base, tj = _image_base(ocr)
    llm = {"drugs": [], "vitals": [{"text": "99", "note": "llm-image"}],
           "structured_vitals": {"spo2": "99"}}
    merged = merge_image(base, llm, ocr)
    v = _txt(_vitals(merged, tj))
    assert (v["vitals_bp_sys"], v["vitals_bp_dia"], v["vitals_spo2"]) == ("130", "80", "99")
    assert v["vitals_temp"] == "NIL"


def test_image_conflict_recorded_and_regex_kept():
    ocr = "BP 130/80\nRepeat 140 90"  # regex reads only line 0; AI digits exist in OCR
    base, tj = _image_base(ocr)
    llm = {"drugs": [], "vitals": [], "structured_vitals": {"sys": "140", "dia": "90"}}
    merged = merge_image(base, llm, ocr)
    v = _txt(_vitals(merged, tj))
    assert (v["vitals_bp_sys"], v["vitals_bp_dia"]) == ("130", "80")
    assert {c["field"] for c in merged.get("vitals_conflicts", [])} == {"sys", "dia"}


def test_image_ai_digits_absent_from_ocr_still_dropped():
    ocr = "BP 130/80"
    base, tj = _image_base(ocr)
    merged = merge_image(base, {"drugs": [], "vitals": [], "structured_vitals": {"spo2": "97"}}, ocr)
    v = _txt(_vitals(merged, tj))
    assert v["vitals_spo2"] == "NIL"  # OCR verification gate unchanged
    assert (v["vitals_bp_sys"], v["vitals_bp_dia"]) == ("130", "80")


def test_image_invented_ai_reading_not_shown_even_as_conflict():
    ocr = "BP 130/80"
    base, tj = _image_base(ocr)
    merged = merge_image(base, {"drugs": [], "vitals": [],
                                "structured_vitals": {"sys": "999", "dia": "99"}}, ocr)
    assert not merged.get("vitals_conflicts")
    v = _vitals(merged, tj)
    assert (v["vitals_bp_sys"]["text"], v["vitals_bp_dia"]["text"]) == ("130", "80")
    assert "999" not in v["vitals_bp_sys"]["note"]


def test_editing_one_vital_keeps_others_and_their_conflict_flags():
    """Editable form (app._apply_edits): correcting temp must not silently
    mark untouched regex BP as human-set (which would hide its AI conflict)."""
    import app
    base, tj = _audio_base("Test patient seen today. BP 130/80.")
    merged = merge_primary(base, {"drugs": [], "vitals": [],
                                  "structured_vitals": {"sys": "140", "dia": "80", "spo2": "99%"}})
    defaults = {s["key"]: s["text"] for s in resolve_slots(merged, tj, "er_discharge")["slots"]}
    patched, audit = app._apply_edits(merged, {"vitals_temp": "99.1 F"}, defaults)
    v = _vitals(patched, tj)
    assert _txt(v) == {"vitals_bp_sys": "130", "vitals_bp_dia": "80",
                       "vitals_temp": "99.1 F", "vitals_spo2": "99%"}
    assert "140" in v["vitals_bp_sys"]["note"]  # conflict still visible
    assert [a["slot_key"] for a in audit] == ["vitals_temp"]  # only the real human change
