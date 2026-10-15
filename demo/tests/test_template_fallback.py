"""Regression: discharge-note template fallback must never produce a misleading note.

Run:  python -m pytest demo/tests/test_template_fallback.py -q

Before: any premium-layout failure (missing coords file, slot resolution
error, ...) fell back to "classic", which re-loaded the PREMIUM layout with no
slot data -> an empty A4 form with header/job/date and no medicines, vitals,
allergies or diagnosis (only a stderr line); missing/corrupt templates raised
raw Jinja errors. Intended now: the premium layout is the only supported
layout; any failure raises TemplateRenderError and returns NO html/docx.
Synthetic note text only; templates are copied to a temp dir before breaking.
"""
import io
import os
import re
import shutil
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO = os.path.dirname(_HERE)
sys.path.insert(0, _DEMO)

import fill_template as ft  # noqa: E402
from coords import resolve_slots  # noqa: E402
from stt_extract import run_text_extract  # noqa: E402

NOTE = ("Test cough. Patient is allergic to penicillin. BP 120/80, temp 98.6 degrees, SpO2 98%. "
        "Give paracetamol 500 mg twice daily for 3 days. Diagnosis: test condition.")


def _note(text=NOTE):
    return run_text_extract(text, [{"id": 0, "text": text, "start": 0.0, "end": 2.0, "lang": "en-IN",
                                    "confidence": 0.9, "words": []}], "t-001", use_llm=False)


def _docx_text(b):
    from docx import Document
    return "\n".join(p.text for p in Document(io.BytesIO(b)).paragraphs)


@pytest.fixture
def tpl(tmp_path, monkeypatch):
    """Copy of demo/templates under tmp; fill_template reads from there."""
    shutil.copytree(os.path.join(_DEMO, "templates"), tmp_path / "templates")
    monkeypatch.setattr(ft, "_HERE", str(tmp_path))
    return tmp_path / "templates"


def _render_error(callable_):
    err = getattr(ft, "TemplateRenderError", None)
    assert err is not None, "fill_template has no TemplateRenderError (silent fallback)"
    with pytest.raises(err) as ei:
        callable_()
    return str(ei.value)


# ---- normal rendering (guards) ----

def test_normal_render_keeps_all_clinical_content():
    tj, ej = _note()
    out = ft.fill_template(ej, tj)
    html, docx = out["html"], _docx_text(out["docx_bytes"])
    for needle in ("paracetamol", "500", "twice daily", "penicillin", "120", "80",
                   "98.6", "98%", "test condition"):
        assert needle in html, needle
        assert needle in docx, needle
    assert "None extracted" not in html and "None extracted" not in docx


def test_html_and_docx_carry_the_same_slot_values():
    tj, ej = _note()
    out = ft.fill_template(ej, tj)
    docx = _docx_text(out["docx_bytes"])
    for s in resolve_slots(ej, tj)["slots"]:
        assert s["text"] in out["html"], s["key"]
        assert s["text"] in docx, s["key"]


def test_unknown_allergy_status_preserved_in_both_formats():
    tj, ej = _note(NOTE.replace("Patient is allergic to penicillin. ", ""))
    out = ft.fill_template(ej, tj)
    assert "NOT DOCUMENTED" in out["html"] and "NOT DOCUMENTED" in _docx_text(out["docx_bytes"])
    assert "No known allergies" not in out["html"]


def test_generated_note_never_claims_verification():
    tj, ej = _note()
    out = ft.fill_template(ej, tj)
    for text in (out["html"], _docx_text(out["docx_bytes"])):
        assert not re.search(r"\bverified\b|\bapproved\b", text, re.I)
    assert "DRAFT" in _docx_text(out["docx_bytes"])


# ---- fallback / failure behaviour ----

def test_missing_coords_file_is_an_error_not_a_blank_form(tpl):
    os.remove(tpl / "er_discharge.coords.json")
    tj, ej = _note()
    msg = _render_error(lambda: ft.fill_template(ej, tj))
    assert "coords" in msg.lower()


def test_slot_resolution_failure_is_an_error_not_a_blank_form(monkeypatch):
    import coords

    def boom(*a, **k):
        raise KeyError("synthetic slot failure")

    monkeypatch.setattr(coords, "resolve_slots", boom)
    tj, ej = _note()
    msg = _render_error(lambda: ft.fill_template(ej, tj))
    assert "synthetic slot failure" in msg


def test_missing_premium_template_is_clear_error(tpl):
    os.remove(tpl / "er_discharge.premium.html.j2")
    tj, ej = _note()
    msg = _render_error(lambda: ft.fill_template(ej, tj))
    assert "er_discharge.premium.html.j2" in msg


def test_corrupt_premium_template_is_clear_error(tpl):
    (tpl / "er_discharge.premium.html.j2").write_text("<html>{% for s in slots %}{{ s.text }</html>",
                                                      encoding="utf-8")
    tj, ej = _note()
    _render_error(lambda: ft.fill_template(ej, tj))


def test_template_that_drops_clinical_content_is_rejected(tpl):
    # renders fine but never prints the slots -> would be an empty, valid-looking note
    (tpl / "er_discharge.premium.html.j2").write_text("<html><h1>ER DISCHARGE {{ job_id }}</h1></html>",
                                                      encoding="utf-8")
    tj, ej = _note()
    msg = _render_error(lambda: ft.fill_template(ej, tj))
    assert "allergy_line" in msg or "missing" in msg.lower()


def test_missing_spec_is_clear_error():
    tj, ej = _note()
    _render_error(lambda: ft.fill_template(ej, tj, "no_such_template"))


def test_docx_failure_keeps_html_and_reports(monkeypatch):
    def boom(ctx):
        raise RuntimeError("synthetic docx failure")

    monkeypatch.setattr(ft, "_docx_word_editable", boom)
    tj, ej = _note()
    out = ft.fill_template(ej, tj)
    assert "penicillin" in out["html"] and "paracetamol" in out["html"]
    assert out["docx_bytes"] is None and "synthetic docx failure" in out["docx_error"]


# ---- callers surface the error, write nothing ----

def test_pipeline_reports_template_failure(tmp_path, monkeypatch, capsys):
    import pipeline
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(pipeline, "_HERE", str(out))
    monkeypatch.setattr(ft, "fill_template",
                        lambda *a, **k: (_ for _ in ()).throw(ft.TemplateRenderError("synthetic")))
    wav = os.path.join(_DEMO, "audio_in", "sample1_hinglish_fever.wav")
    monkeypatch.setattr(sys, "argv", ["pipeline.py", "--in", wav, "--key", "demo-001",
                                      "--model", "mock", "--no-llm", "--no-denoise"])
    rc = pipeline.main()
    err = capsys.readouterr().err
    assert rc != 0 and "NOT generated" in err
    assert not (out / "exports").exists() or not os.listdir(out / "exports")


# ---- app: a broken layout never becomes a "verified" export ----

from test_verification_state import _apply, _click, _job, _start, _verify, env  # noqa: E402,F401


def test_app_layout_failure_blocks_verified_exports(env, tmp_path, monkeypatch):
    _, out = env
    at = _start()
    _click(at, "▶ Run pipeline")
    job = _job(at)
    _apply(at, job, drug_0_dose="650")
    broken = tmp_path / "broken"
    shutil.copytree(os.path.join(_DEMO, "templates"), broken / "templates")
    os.remove(broken / "templates" / "er_discharge.coords.json")
    monkeypatch.setattr(ft, "_HERE", str(broken))  # layout breaks before Verify
    _verify(at, job)
    errors = " ".join(str(e.value) for e in at.error)
    assert "could not be generated" in errors and "NOT generated" in errors
    exports = out / "exports"
    assert not any(p.name.startswith(f"{job}.corrected") for p in exports.iterdir())
