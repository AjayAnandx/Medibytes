"""Regression: stale corrected exports + prior-run corrections (verification safety).

Run:  python -m pytest demo/tests/test_verification_exports.py -q

A) exports/{job}.corrected.html/.docx must only sit at their canonical path
   while they match the current verified note. Outdated ones are moved to a
   "superseded" archive name (kept, never deleted) and the move is audited.
B) Only corrections made in the current processing/editing run count toward
   the Verify requirement; earlier runs stay in the audit history.
Reuses the AppTest harness of test_verification_state (synthetic note text,
fake uploader/STT/OCR, app._HERE -> temp dir; no patient data).
"""
import glob
import json
import os

from test_verification_state import (  # noqa: F401  (env fixture used by name)
    OCR_LINES, _apply, _click, _job, _needs_reverify, _start, _trail, _verified, _verify, env,
)


def _canon(out, job):
    return out / "exports" / f"{job}.corrected.html", out / "exports" / f"{job}.corrected.docx"


def _archived(out, job, ext="html"):
    return sorted(glob.glob(str(out / "exports" / f"{job}.corrected.superseded-*.{ext}")))


def _verify_btn(at, job):
    return next(b for b in at.button if b.key == f"verify_{job}")


def _caption(at):
    return next(str(c.value) for c in at.caption if str(c.value).startswith("Audited corrections"))


def _verified_with_dose(env, dose="650"):
    _, out = env
    at = _start()
    _click(at, "▶ Run pipeline")
    job = _job(at)
    _apply(at, job, drug_0_dose=dose)
    _verify(at, job)
    html, docx = _canon(out, job)
    assert html.exists() and docx.exists()
    assert dose in html.read_text(encoding="utf-8")
    return at, job, out


# ---- A: stale corrected exports ----

def test_verified_exports_have_manifest_matching_note(env):
    at, job, out = _verified_with_dose(env)
    man = json.loads((out / "exports" / f"{job}.corrected.verified.json").read_text(encoding="utf-8"))
    ver = [e for e in _trail(out, job) if e.get("event") == "verified"][-1]
    assert man["note_fingerprint"] == ver["note_fingerprint"]
    assert man["status"] == "verified"


def test_edit_after_verify_moves_exports_out_of_canonical_path(env):
    at, job, out = _verified_with_dose(env)
    old = _canon(out, job)[0].read_text(encoding="utf-8")
    _apply(at, job, vitals_bp_sys="135")
    assert _needs_reverify(at)
    assert not any(p.exists() for p in _canon(out, job))  # no stale "current" export
    arch = _archived(out, job)
    assert len(arch) == 1 and len(_archived(out, job, "docx")) == 1
    assert open(arch[0], encoding="utf-8").read() == old  # history kept unchanged
    ev = [e for e in _trail(out, job) if e.get("event") == "corrected_exports_superseded"]
    assert ev and ev[-1]["archived"]


def test_undo_after_verify_also_supersedes(env):
    at, job, out = _verified_with_dose(env)
    _apply(at, job, drug_0_dose="500")
    assert not _canon(out, job)[0].exists() and len(_archived(out, job)) == 1


def test_reverify_writes_new_exports_and_keeps_archive(env):
    at, job, out = _verified_with_dose(env)
    _apply(at, job, drug_0_dose="1000")
    _verify(at, job)
    html = _canon(out, job)[0]
    assert html.exists() and "1000" in html.read_text(encoding="utf-8")
    assert len(_archived(out, job)) == 1 and "650" in open(_archived(out, job)[0], encoding="utf-8").read()


def test_harmless_rerun_keeps_current_exports(env):
    at, job, out = _verified_with_dose(env)
    at.run()
    _click(at, "Apply corrections")  # no change
    assert _verified(at) and _canon(out, job)[0].exists() and _archived(out, job) == []


def test_audio_rerun_supersedes_exports(env):
    at, job, out = _verified_with_dose(env)
    _click(at, "▶ Run pipeline")
    assert not _verified(at)
    assert not _canon(out, job)[0].exists() and len(_archived(out, job)) == 1


def test_stale_exports_from_earlier_session_superseded_on_load(env):
    _, job, out = _verified_with_dose(env)
    at2 = _start()  # new browser session, same file; nothing verified here
    _click(at2, "▶ Run pipeline")
    assert _job(at2) == job and not _verified(at2)
    assert not _canon(out, job)[0].exists() and len(_archived(out, job)) == 1


def test_legacy_export_without_manifest_superseded_not_deleted(env):
    _, out = env
    at = _start()
    _click(at, "▶ Run pipeline")
    job = _job(at)
    legacy = _canon(out, job)[0]
    legacy.write_text("<html>legacy corrected note</html>", encoding="utf-8")
    at.run()
    assert not legacy.exists()
    assert [open(p, encoding="utf-8").read() for p in _archived(out, job)] == ["<html>legacy corrected note</html>"]


def test_switching_upload_leaves_other_jobs_exports(env):
    configure, _ = env
    at, job_a, out = _verified_with_dose(env)
    configure(name="synthetic_note_b.wav", freq=880)
    at.run()
    _click(at, "▶ Run pipeline")
    assert _job(at) != job_a
    assert _canon(out, job_a)[0].exists()  # A's verified export untouched by B


# ---- B: corrections from previous runs ----

def test_audio_rerun_does_not_count_previous_corrections(env):
    _, out = env
    at = _start()
    _click(at, "▶ Run pipeline")
    job = _job(at)
    _apply(at, job, drug_0_dose="650")
    assert _caption(at).startswith("Audited corrections: 1")
    _click(at, "▶ Run pipeline")  # new processing run, same file
    assert _caption(at).startswith("Audited corrections: 0")
    assert _verify_btn(at, job).disabled
    assert len([e for e in _trail(out, job) if e.get("slot_key")]) == 1  # history kept
    _apply(at, job, drug_0_dose="650")
    assert _caption(at).startswith("Audited corrections: 1")
    runs = {e["run_id"] for e in _trail(out, job) if e.get("slot_key") and e.get("run_id")}
    assert len(runs) == 2


def test_new_session_does_not_count_earlier_session_corrections(env):
    _, out = env
    at = _start()
    _click(at, "▶ Run pipeline")
    job = _job(at)
    _apply(at, job, drug_0_dose="650")
    at2 = _start()
    _click(at2, "▶ Run pipeline")
    assert _caption(at2).startswith("Audited corrections: 0")
    assert _verify_btn(at2, job).disabled


def test_legacy_entries_without_run_id_not_counted(env):
    _, out = env
    at = _start()
    _click(at, "▶ Run pipeline")
    job = _job(at)
    (out / "entities" / f"{job}.audit.json").write_text(json.dumps([
        {"ts": "2026-01-01T00:00:00", "slot_key": "drug_0_dose", "entity_path": "drugs[0].dose",
         "original_ai": 500.0, "human_corrected": 650.0}]), encoding="utf-8")
    at.run()
    assert _caption(at).startswith("Audited corrections: 0")
    assert _verify_btn(at, job).disabled


def test_ocr_rerun_does_not_count_previous_corrections(env):
    at = _start(image=True)
    _click(at, "▶ Run OCR")
    job = _job(at)
    _apply(at, job, dx_code_box="Z00.0")
    assert _caption(at).startswith("Audited corrections: 1")
    _click(at, "▶ Run OCR")
    assert _caption(at).startswith("Audited corrections: 0")
    assert _verify_btn(at, job).disabled
