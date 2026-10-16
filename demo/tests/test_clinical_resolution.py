"""Regression: unrecognised medication mentions need explicit clinician resolution;
other vitals (heart rate) are editable and audited.

Run:  python -m pytest demo/tests/test_clinical_resolution.py -q

Found on 2026-10-16: an unrecognised medication mention ("nitrofuurentoin 100 mg")
only blocked Verify when no recognised medicine existed; with one recognised
medicine on the note it could be verified without anyone deciding about it.
Heart rate and other box-less vitals were shown but could not be corrected.
Synthetic text only; temp output folders; no LLM.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from coords import resolve_slots  # noqa: E402
from fill_template import fill_template  # noqa: E402
from stt_extract import run_text_extract  # noqa: E402
from test_discharge_report import _docx_text, _visible  # noqa: E402
from test_verification_state import _click, _job, _start, _trail, env  # noqa: E402,F401

MIXED = ("Fever and burning urine for three days. BP 128/82. Temp 99.4 degrees Fahrenheit. SpO2 98% on room air. "
         "heart rate of 96. Give paracetamol 500 mg twice daily for 3 days. "
         "Give nitrofuurentoin 100 mg twice daily for 5 days. No known allergies. "
         "Diagnosis: urinary tract infection. Follow up in one week.")


def _ex(text=MIXED):
    import app
    tj, ej = run_text_extract(text, [{"id": 0, "text": text, "start": 0.0, "end": 5.0}], "t-cr", use_llm=False)
    d, _ = app._slot_defaults(ej, tj)
    return app, tj, ej, d


def _unrec(ej, tj):
    return [s for s in resolve_slots(ej, tj)["slots"] if s["key"].startswith("unrec_")]


# ---- function level ----

def test_unrecognised_mention_is_red_even_with_a_recognised_medicine():
    app, tj, ej, d = _ex()
    assert [x["name"] for x in ej["drugs"]] == ["paracetamol"]
    (u,) = _unrec(ej, tj)
    assert u["color"] == "RED" and "nitrofuurentoin 100 mg" in u["text"]  # was: no RED, Verify allowed
    out = fill_template(ej, tj)
    for text in (_visible(out["html"]), _docx_text(out["docx_bytes"])):
        assert "nitrofuurentoin 100 mg" in text and "clinician must resolve" in text


def test_dismiss_resolution_is_audited_and_clears_red():
    app, tj, ej, d = _ex()
    patched, audit = app._apply_edits(ej, {"unrec_0": "RESOLVED: dismissed"}, d)
    (u,) = _unrec(patched, tj)
    assert u["color"] == "YELLOW" and "RESOLVED: not a prescribed medicine" in u["text"]
    assert {"slot_key": "unrec_0", "original_ai": "unresolved", "human_corrected": "dismissed"}.items() \
        <= audit[0].items()
    assert "nitrofuurentoin" in _visible(fill_template(patched, tj)["html"])  # mention still visible


def test_added_resolution_requires_an_added_medicine_row():
    app, tj, ej, d = _ex()
    keys = app._editable_keys(d)
    assert "drug_1_name" in keys and keys.index("drug_1_duration") < keys.index("unrec_0")  # blank add row
    p1, _ = app._apply_edits(ej, {"unrec_0": "RESOLVED: added"}, d)
    assert _unrec(p1, tj)[0]["color"] == "RED"  # claimed "added" but nothing was added
    p2, _ = app._apply_edits(ej, {"unrec_0": "RESOLVED: added", "drug_1_name": "nitrofurantoin",
                                  "drug_1_dose": "100", "drug_1_unit": "mg"}, d)
    assert _unrec(p2, tj)[0]["color"] == "YELLOW"
    assert [x["name"] for x in p2["drugs"]] == ["paracetamol", "nitrofurantoin"]


def test_no_unrecognised_mention_means_no_extra_form_rows():
    app, tj, ej, d = _ex("Cough. Give paracetamol 500 mg twice daily for 3 days.")
    assert not [k for k in app._editable_keys(d) if k.startswith(("unrec_", "drug_1"))]


def test_other_vitals_are_editable_and_audited():
    app, tj, ej, d = _ex()
    assert d["vitals_other"] == "heart rate of 96"
    assert "vitals_other" in app._editable_keys(d)
    patched, audit = app._apply_edits(ej, {"vitals_other": "heart rate 92/min; RR 18/min"}, d)
    assert [(a["slot_key"], a["original_ai"], a["human_corrected"]) for a in audit] == \
        [("vitals_other", "heart rate of 96", "heart rate 92/min; RR 18/min")]
    r = resolve_slots(patched, tj)
    assert r["other_vitals"] == ["heart rate 92/min", "RR 18/min"]
    s = {x["key"]: x["text"] for x in r["slots"]}
    assert s["vitals_bp_sys"] == "128" and s["vitals_spo2"] == "98%"  # boxed vitals untouched
    out = fill_template(patched, tj)
    assert "heart rate 92/min; RR 18/min" in _visible(out["html"])
    assert "heart rate 92/min; RR 18/min" in _docx_text(out["docx_bytes"])


def test_other_vitals_can_be_added_when_none_extracted():
    app, tj, ej, d = _ex("Cough. BP 120/80. Give paracetamol 500 mg twice daily for 3 days.")
    assert d["vitals_other"] == ""
    patched, _ = app._apply_edits(ej, {"vitals_other": "pulse 76/min"}, d)
    assert resolve_slots(patched, tj)["other_vitals"] == ["pulse 76/min"]


# ---- through the real Streamlit editor ----

def _verify_btn(at, job):
    return next(b for b in at.button if b.key == f"verify_{job}")


def test_app_unresolved_mention_blocks_verify_until_resolved(env):
    configure, out = env
    configure(note=MIXED, icd="N39.0")
    at = _start()
    _click(at, "▶ Run pipeline")
    job = _job(at)
    # a correction alone is not enough while the mention is unresolved
    at.text_area(key=f"{job}_vitals_other").set_value("heart rate 92/min")
    _click(at, "Apply corrections")
    assert _verify_btn(at, job).disabled
    assert any("unrecognised medication mention(s) unresolved" in str(e.value) for e in at.error)
    at.selectbox(key=f"{job}_unrec_0").set_value("Not a prescribed medicine — dismiss")
    _click(at, "Apply corrections")
    assert not _verify_btn(at, job).disabled
    _verify_btn(at, job).click().run()
    assert not at.exception
    keys = {e["slot_key"]: e for e in _trail(out, job) if e.get("slot_key")}
    assert keys["unrec_0"]["human_corrected"] == "dismissed"
    assert keys["vitals_other"]["human_corrected"] == "heart rate 92/min"
    html = _visible((out / "exports" / f"{job}.corrected.html").read_text(encoding="utf-8"))
    assert "CLINICIAN-VERIFIED" in html and "RESOLVED: not a prescribed medicine" in html
    assert "heart rate 92/min" in html


def test_app_changing_resolution_after_verify_invalidates(env):
    configure, out = env
    configure(note=MIXED, icd="N39.0")
    at = _start()
    _click(at, "▶ Run pipeline")
    job = _job(at)
    at.selectbox(key=f"{job}_unrec_0").set_value("Not a prescribed medicine — dismiss")
    _click(at, "Apply corrections")
    _verify_btn(at, job).click().run()
    assert any("Verified — corrected exports unlocked" in str(s.value) for s in at.success)
    at.selectbox(key=f"{job}_unrec_0").set_value("Unresolved — clinician must decide")
    _click(at, "Apply corrections")
    assert any("re-verif" in str(w.value).lower() for w in at.warning)
    assert _verify_btn(at, job).disabled  # back to RED
    assert any(e.get("event") == "verification_invalidated" for e in _trail(out, job))


# ---- denied symptoms (found in the 2026-10-16 visual check: "denies chest pain and
# vomiting" printed vomiting under "Other symptoms"; only 2 symptoms could be denied) ----

def _sym(text):
    tj, ej = run_text_extract(text, [{"id": 0, "text": text, "start": 0.0, "end": 5.0}], "t-neg", use_llm=False)
    return [(s["text"], bool(s.get("negated"))) for s in ej["symptoms"]], tj, ej


def test_every_denied_symptom_is_marked_denied():
    assert _sym("Fever and headache. Patient denies chest pain and vomiting.")[0] == \
        [("fever", False), ("headache", False), ("chest pain", True), ("vomiting", True)]
    assert _sym("Fever, no vomiting.")[0] == [("fever", False), ("vomiting", True)]
    assert _sym("Cough without fever.")[0] == [("fever", True), ("cough", False)]
    assert _sym("No fever but has cough.")[0] == [("fever", True), ("cough", False)]


def test_denial_does_not_leak_to_present_symptoms():
    assert _sym("Fever with headache and vomiting. Patient denies chest pain.")[0] == \
        [("fever", False), ("headache", False), ("vomiting", False), ("chest pain", True)]
    assert _sym("Fever for 3 days, no known allergies.")[0] == [("fever", False)]
    assert _sym("Cannot sleep due to cough.")[0] == [("cough", False)]


def test_denied_symptom_printed_as_denied_on_the_note():
    _, tj, ej = _sym("Fever and headache. Patient denies chest pain and vomiting. "
                     "Give paracetamol 500 mg twice daily for 3 days.")
    vis = _visible(fill_template(ej, tj)["html"])
    other = vis[vis.index("Other symptoms"):vis.index("Denied symptoms")]
    assert "vomiting" not in other
    assert "vomiting" in vis[vis.index("Denied symptoms"):vis.index("Medications")]
