"""Regression: extraction defects found with REAL faster-whisper output (2026-10-16).

Run:  python -m pytest demo/tests/test_extraction_realworld.py -q

Inputs are the text of real faster-whisper `base` transcripts of the repo's
synthetic text-to-speech clips (demo scripts/*.mp3) - no audio or model needed.
Found: fuzzy match turned "peridine" (phenazopyridine) into cetirizine;
unknown medicines (nitrofurantoin) dropped silently; "once a day" not a
frequency; "99.2 Fahrenheit" not a temperature; heart rate never shown;
filler word "in" taken as the drug name; diagnosis text starting with "is".
"""
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO = os.path.dirname(_HERE)
sys.path.insert(0, _DEMO)

from coords import resolve_slots  # noqa: E402
from fill_template import fill_template  # noqa: E402
from stt_extract import run_text_extract  # noqa: E402

UTI = ("Patient presents today reporting a three-day history of dischuria. On examination, vitals are stable with "
       "a blood pressure of 118 over 74, temperature at 99.2 Fahrenheit, and heart rate of 82. The primary clinical "
       "diagnosis is an uncomplicated UTI. The treatment plan includes nitrofuurentoin 100 mg twice daily for five "
       "days, and phenyzo peridine 200 mg three times a day for two days for symptom relief.")
BRONCHITIS = ("Based on that, the clinical diagnosis is acute bronchitis. We are starting them on acytromycin, "
              "500 mg once a day for 3 days, along with paracetamol, 500 mg twice a day for 3 days.")
HINGLISH = "Khansi hai, cough for 5 days, BPA 130 by AC, give as a thromise in 500 mg once daily 3 days."


def _run(text, source="audio"):
    segs = [{"id": i, "text": t, "start": 0.0, "end": 0.0, "lang": "", "confidence": 0.9, "words": []}
            for i, t in enumerate(text.split("\n"))]
    tj, ej = run_text_extract(text, segs, "t-rw", use_llm=False, source=source)
    return tj, ej, resolve_slots(ej, tj)


def test_fuzzy_match_never_substitutes_a_different_drug():
    _, ej, r = _run(UTI)
    names = [d["name"] for d in ej["drugs"]]
    assert "cetirizine" not in names  # was: "peridine" -> cetirizine 200 mg
    assert any("peridine 200 mg" in u for u in r["unrecognised_meds"])


def test_unrecognised_medicine_is_surfaced_not_dropped():
    tj, ej, r = _run(UTI)
    assert any("nitrofuurentoin 100 mg twice daily" in u for u in r["unrecognised_meds"])
    slots = {s["key"]: s for s in r["slots"]}
    assert slots["drug_0_name"]["color"] == "RED"
    assert "no recognised medicine" in slots["drug_0_name"]["text"]  # not "no medicine documented"
    assert any("MEDICATION NOT RECOGNISED" in n for n in r["review_notes"])
    out = fill_template(ej, tj)
    from docx import Document
    import io
    docx = "\n".join(p.text for p in Document(io.BytesIO(out["docx_bytes"])).paragraphs)
    for text in (out["html"], docx):
        assert "nitrofuurentoin 100 mg" in text and "Possible medications not recognised" in text


def test_once_a_day_is_a_frequency():
    _, ej, _ = _run(BRONCHITIS)
    by = {d["name"]: d for d in ej["drugs"]}
    assert by["azithromycin"]["frequency"] == "once a day"  # was empty
    assert by["paracetamol"]["frequency"] == "twice a day"


def test_filler_word_before_dose_uses_the_drug_word():
    _, ej, _ = _run(HINGLISH)
    (d,) = ej["drugs"]
    assert d["name"] == "azithromycin" and d["dose"] == 500.0 and d["frequency"] == "once daily"
    assert d["color"] == "YELLOW" and "fuzzy thromise->azithromycin" in d["note"]


@pytest.mark.parametrize("heard,drug", [("asitromycin", "azithromycin"), ("parasitamol", "paracetamol"),
                                        ("ibuprofin", "ibuprofen"), ("amoxycillin", "amoxicillin")])
def test_genuine_misspellings_still_matched_for_review(heard, drug):
    _, ej, _ = _run(f"Give {heard} 500 mg twice daily for 3 days.")
    (d,) = ej["drugs"]
    assert d["name"] == drug and d["color"] == "YELLOW"


def test_temperature_with_unit_word_but_no_degrees():
    _, _, r = _run(UTI)
    slots = {s["key"]: s["text"] for s in r["slots"]}
    assert slots["vitals_temp"] == "99.2 Fahrenheit"  # was NIL
    assert (slots["vitals_bp_sys"], slots["vitals_bp_dia"]) == ("118", "74")


def test_heart_rate_is_shown_not_dropped():
    tj, ej, r = _run(UTI)
    assert r["other_vitals"] == ["heart rate of 82"]
    assert "Other recorded vitals" in fill_template(ej, tj)["html"]


def test_diagnosis_text_without_leading_verb():
    _, _, r = _run(UTI)
    assert {s["key"]: s["text"] for s in r["slots"]}["dx_text"] == "an uncomplicated UTI"


def test_ocr_unknown_medicine_listed_without_guessing():
    _, ej, r = _run("Patient Name: Test Person\nTab Nitrofurantoin 100 mg twice daily\nBP 120/80", source="image")
    assert ej["drugs"] == []
    assert any("Nitrofurantoin 100 mg" in u for u in r["unrecognised_meds"])


def test_negated_dose_mention_is_not_a_review_item():
    _, ej, r = _run("No need for nitrofurantoin 100 mg today. Give paracetamol 500 mg twice daily for 3 days.")
    assert [d["name"] for d in ej["drugs"]] == ["paracetamol"]
    assert r["unrecognised_meds"] == []


# ---- found with real EasyOCR on synthetic prescription images ----

@pytest.mark.parametrize("source", ["audio", "image"])
def test_number_word_frequency_survives_digitisation(source):
    # "three times a day" is digitised to "3 times a day" before matching
    _, ej, _ = _run("Amoxicillin 500 mg three times a day for 5 days", source=source)
    (d,) = ej["drugs"]
    assert d["frequency"] == "3 times a day" and d["duration"] == "5 days"


def test_ocr_temperature_with_bare_unit_letter_shows_value_only():
    _, _, r = _run("Patient Name: Test Person\nTemp: 99.2 F\nParacetamol 500 mg", source="image")
    assert {s["key"]: s["text"] for s in r["slots"]}["vitals_temp"] == "99.2 F"  # was "Temp: 99.2 F"


def test_ocr_medication_line_with_misread_dose_is_not_lost():
    _, ej, r = _run("Paracetamol 500 mg\nNitrofurantoin 1DD mg twice daily\nAllergies: penicillin", source="image")
    assert [d["name"] for d in ej["drugs"]] == ["paracetamol"]
    assert "Nitrofurantoin 1DD mg twice daily" in r["unrecognised_meds"]  # was only in the source text


def test_ocr_parsed_lines_are_not_duplicated_as_review_items():
    _, ej, r = _run("Tab. Paracetamol 500 mg\nTwice daily\nAmoxicillin 250 mg three times a day", source="image")
    assert [d["name"] for d in ej["drugs"]] == ["paracetamol", "amoxicillin"]
    assert r["unrecognised_meds"] == []
