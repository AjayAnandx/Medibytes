"""Regression: duplicate vital readings (Bug #5).

Run:  python -m pytest demo/tests/test_vitals_duplicates.py -q

Remaining case after Bug #2: SpO2 dictated/printed as "NN percent" is
extracted by regex but was not recognised as SpO2 by the ER classifiers, so
the note box stayed NIL and the AI's "NN%" was added as a second copy of the
same reading. Synthetic inputs only, no Ollama.
Also guards: genuinely different / repeated readings are not collapsed.
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


def _base(text, source="audio"):
    segs = [{"id": i, "text": t, "start": 0.0, "end": 0.0, "lang": "", "confidence": 0.9,
             "words": []} for i, t in enumerate(text.split("\n"))]
    norm = normalize_text(text, source=source)
    ent = extract_entities(text, norm["normalized_en"], segs, source=source)
    tj = {"job_id": "t", "text": text, "normalized_en": norm["normalized_en"], "segments": segs}
    if source == "image":
        tj["source"] = "image"
    return ent, tj


def _box(ent, tj, key):
    return next(s for s in resolve_slots({"job_id": "t", **ent}, tj, "er_discharge")["slots"]
                if s["key"] == key)


def _spo2_items(ent):
    return [v["text"] for v in ent.get("vitals", [])
            if "99" in v["text"] or "97" in v["text"]]


def test_regex_percent_spo2_fills_box_regex_only():
    ent, tj = _base("Saturation 99 percent on room air.")
    assert [v["text"] for v in ent["vitals"]] == ["99 percent on room air"]
    box = _box(ent, tj, "vitals_spo2")
    assert box["text"] == "99 percent" and box["color"] == "YELLOW"  # was NIL / RED


def test_oxygen_percent_wording():
    ent, tj = _base("Oxygen 97 percent.")
    assert _box(ent, tj, "vitals_spo2")["text"].startswith("97")


def test_ai_same_spo2_not_added_twice():
    ent, tj = _base("Saturation 99 percent on room air.")
    merged = merge_primary(ent, {"drugs": [], "vitals": [{"text": "99%"}],
                                 "structured_vitals": {"spo2": "99%"}})
    assert _spo2_items(merged) == ["99 percent on room air"]  # no extra "99%"
    assert "spo2" not in merged.get("structured_vitals", {})
    assert not merged.get("vitals_conflicts")
    assert _box(merged, tj, "vitals_spo2")["text"] == "99 percent"


def test_ai_different_spo2_is_conflict_not_second_reading():
    ent, tj = _base("Saturation 99 percent on room air.")
    merged = merge_primary(ent, {"drugs": [], "vitals": [{"text": "97%"}],
                                 "structured_vitals": {"spo2": "97%"}})
    assert _spo2_items(merged) == ["99 percent on room air"]
    conf = {c["field"]: c for c in merged.get("vitals_conflicts", [])}
    assert conf["spo2"]["ai"] == "97%"
    box = _box(merged, tj, "vitals_spo2")
    assert box["text"] == "99 percent" and "97%" in box["note"]


def test_image_percent_spo2_not_duplicated():
    ocr = "Name: Test Person\nSaturation 99 percent"
    ent, tj = _base(ocr, source="image")
    merged = merge_image(ent, {"drugs": [], "vitals": [{"text": "99", "note": "llm-image"}],
                               "structured_vitals": {"spo2": "99"}}, ocr)
    assert _spo2_items(merged) == ["99 percent"]
    assert "spo2" not in merged.get("structured_vitals", {})
    assert _box(merged, tj, "vitals_spo2")["text"] == "99 percent"


def test_repeated_and_different_readings_are_not_collapsed():
    # two dictated measurements are separate source facts, not duplicates
    ent, _ = _base("BP 130/80 on arrival. Repeat BP 130/80 after rest.")
    assert [v["text"] for v in ent["vitals"]] == ["BP 130/80", "BP 130/80"]
    assert len({v["source_sentence"] for v in ent["vitals"]}) == 2
    ent2, _ = _base("BP 150/95 on arrival. Repeat BP 130/80 after rest.")
    assert [v["text"] for v in ent2["vitals"]] == ["BP 150/95", "BP 130/80"]
