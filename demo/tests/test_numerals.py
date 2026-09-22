"""Golden numeral tests: medication + vitals extraction (regex path, no Ollama).

Run:  python -m pytest demo/tests/test_numerals.py -q
Covers: decimals, BP 'of'/long-form, BPA/AC phonetic (narrow), multi-drug
split, freq vocab (twice a day / every 8 hours), generic allergy candidate.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO = os.path.dirname(_HERE)
sys.path.insert(0, _DEMO)

from stt_extract import extract_entities, normalize_text
from coords import resolve_slots


def _run(text):
    norm = normalize_text(text)
    segs = [{"id": 0, "text": text, "start": 0.0, "end": 5.0,
             "lang": "mix", "confidence": 0.9, "words": []}]
    ent = extract_entities(text, norm["normalized_en"], segs)
    tj = {"job_id": "t", "text": text, "language": "mix",
          "segments": segs, "normalized_en": norm["normalized_en"]}
    ent = {"job_id": "t", **ent}
    slots = {s["key"]: s for s in resolve_slots(ent, tj, "er_discharge")["slots"]}
    return ent, slots


def test_demo001_temp_only_no_invented_bp():
    ent, slots = _run(
        "Patient ko fever hai, bukhar 101 degree, give paracetamol 500 mg BID, do time, 3 din tak. No allergy.")
    assert slots["vitals_temp"]["text"] == "101 degree"
    assert slots["vitals_bp_sys"]["text"].startswith("NIL")
    assert slots["vitals_bp_dia"]["text"].startswith("NIL")
    assert ent["drugs"][0]["name"] == "paracetamol"
    assert ent["drugs"][0]["dose"] == 500.0


def test_demo002_bp_split():
    ent, slots = _run(
        "Khansi hai, cough for 5 days, BP 130 by 80, give azithromycin 500 mg once daily 3 days.")
    assert slots["vitals_bp_sys"]["text"] == "130"
    assert slots["vitals_bp_dia"]["text"] == "80"
    assert ent["drugs"][0]["frequency"] == "once daily"
    assert ent["drugs"][0]["duration"] == "3 days"


def test_luvvoice_bpa_ac_phonetic_narrow():
    ent, slots = _run(
        "Khansi hai, cough for 5 days, BPA 130 by AC, give asitromaisin 500 mg once daily 3 days.")
    assert slots["vitals_bp_sys"]["text"] == "130"
    assert slots["vitals_bp_dia"]["text"] == "80"
    assert ent["drugs"][0]["name"] == "azithromycin"


def test_live_two_drugs_freq_and_vitals():
    ent, slots = _run(
        "Looking at their vitals, their blood pressure is sitting at 122 of 78. "
        "Their temp is currently 100.4 degrees Fahrenheit, and their oxygen saturation is "
        "looking good at 99% on room air. We are starting them on amoxicillin, 875 mg "
        "twice a day for seven days, along with ibuprofen, for 100 mg every eight hours as needed.")
    names = [d["name"] for d in ent["drugs"]]
    assert "amoxicillin" in names
    assert "ibuprofen" in names
    amox = next(d for d in ent["drugs"] if d["name"] == "amoxicillin")
    ibu = next(d for d in ent["drugs"] if d["name"] == "ibuprofen")
    assert amox["dose"] == 875.0
    assert amox["frequency"] == "twice a day"
    assert amox["duration"] == "7 days"
    assert ibu["dose"] == 100.0
    assert "8 hours" in ibu["frequency"] or "eight hours" in ibu["frequency"]
    assert slots["vitals_bp_sys"]["text"] == "122"
    assert slots["vitals_bp_dia"]["text"] == "78"
    assert "100.4" in slots["vitals_temp"]["text"]
    assert "99" in slots["vitals_spo2"]["text"]


def test_live_allergy_candidate():
    ent, _ = _run("They don't have any known allergy to a moxasillin.")
    assert any(a["text"] == "moxasillin" and a["negated"] for a in ent["allergies"])
