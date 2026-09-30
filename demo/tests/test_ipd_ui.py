"""IPD P1 step 9: Streamlit UI orchestration + rerun safety.

Run:  python -m unittest demo/tests/test_ipd_ui.py -v   (from repo root)
Helper tests use an in-memory store; the AppTest runs of demo/ipd_app.py use a
database in a temporary directory OUTSIDE the repository (removed afterwards).
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO = os.path.dirname(_HERE)
_REPO = os.path.dirname(_DEMO)
sys.path.insert(0, _DEMO)

import llm_extract  # noqa: E402
import stt_extract  # noqa: E402
from ipd import ui  # noqa: E402
from ipd.models import IST, EventCategory, Role  # noqa: E402
from ipd.store import IpdStore  # noqa: E402
from ipd.timeline import Timeline  # noqa: E402

T0 = datetime(2026, 10, 6, 8, 0, tzinfo=IST)
H = timedelta(hours=1)
NOTE = ("Patient has fever. No chest pain.\nBP 118/76, pulse 82.\nTemperature: 101 F\n"
        "Diagnosis: viral fever.\nStart paracetamol 650 mg TDS.")


def _boom(*a, **k):
    raise AssertionError("LLM/Ollama must not be called")


import subprocess as _subprocess  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402  (imported before any trap is installed)

_REAL_RUN = _subprocess.run


def _run_guard(args, *a, **k):
    """Fail on any ollama invocation; let unrelated OS calls (e.g. platform detection) through."""
    cmd = " ".join(map(str, args)) if isinstance(args, (list, tuple)) else str(args)
    if "ollama" in cmd.lower():
        raise AssertionError("LLM/Ollama must not be called")
    return _REAL_RUN(args, *a, **k)


def trap_llm(test):
    for p in (mock.patch.object(llm_extract, "ollama_available", _boom),
              mock.patch.object(llm_extract, "extract_llm_primary", _boom),
              mock.patch.object(stt_extract, "ollama_tidy", _boom), mock.patch("subprocess.run", _run_guard)):
        p.start()
        test.addCleanup(p.stop)


def counts(store):
    return {t: store._conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("patients", "encounters", "captures", "events", "documents", "document_versions", "audit_log")}


class HelperBase(unittest.TestCase):
    def setUp(self):
        trap_llm(self)
        self.store = IpdStore()
        o = ui.admit_demo_patient(self.store, name="Demo Patient U01", mrn="DEMO-U01", dob=date(1980, 1, 1), sex="F",
                                  ward="Ward 4", bed="12", admitted_by="u_dr_demo", admitted_role=Role.CONSULTANT,
                                  admit_at=T0)
        self.assertTrue(o.ok, o.message)
        self.enc = o.data["encounter_id"]

    def tearDown(self):
        self.store.close()

    def round(self, text=NOTE, at=T0 + 2 * H, who="u_dr_demo", role=Role.CONSULTANT):
        return ui.process_ward_round(self.store, encounter_id=self.enc, text=text, author_id=who, author_role=role,
                                     captured_at=at)

    def gen(self, at=T0 + 3 * H):
        return ui.generate_note(self.store, self.enc, generated_by="u_dr_demo", generated_role=Role.CONSULTANT,
                                generated_at=at)


class TestDbPath(unittest.TestCase):
    def test_default_outside_repo_and_repo_paths_refused(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MEDIBYTES_IPD_DB", None)
            p = ui.default_db_path()
        self.assertFalse(ui._inside(p, _REPO))
        self.assertTrue(p.endswith(os.path.join("MediBytes", "ipd_demo.sqlite")))
        with mock.patch.dict(os.environ, {"MEDIBYTES_IPD_DB": os.path.join(_DEMO, "ipd.sqlite")}):
            with self.assertRaises(ui.UiError):
                ui.default_db_path()
        with self.assertRaises(ui.UiError):
            ui.open_store(os.path.join(_REPO, "x.sqlite"))
        self.assertFalse(os.path.exists(os.path.join(_REPO, "x.sqlite")))


class TestAdmission(HelperBase):
    def test_uses_existing_admission_service(self):
        with mock.patch.object(ui, "admit_patient", wraps=ui.admit_patient) as spy:
            o = ui.admit_demo_patient(self.store, name="Demo Patient U02", mrn="DEMO-U02", dob=None, sex="",
                                      ward="Ward 5", bed="1", admitted_by="u_dr_demo", admitted_role=Role.RESIDENT)
        self.assertTrue(o.ok)
        spy.assert_called_once()

    def test_no_duplicate_admission(self):
        before = counts(self.store)
        o = ui.admit_demo_patient(self.store, name="Demo Patient U01", mrn="DEMO-U01", dob=None, sex="",
                                  ward="Ward 4", bed="12", admitted_by="u_dr_demo", admitted_role=Role.CONSULTANT)
        self.assertTrue(o.ok and o.data["duplicate"])
        self.assertEqual(o.data["encounter_id"], self.enc)
        self.assertEqual(counts(self.store), before)

    def test_synthetic_mrn_required(self):
        o = ui.admit_demo_patient(self.store, name="Real Person", mrn="MRN-12345", dob=None, sex="", ward="W",
                                  bed="1", admitted_by="u", admitted_role=Role.CONSULTANT)
        self.assertFalse(o.ok)
        self.assertIsNone(self.store.get_patient_by_mrn("MRN-12345"))

    def test_context_lookup(self):
        c = ui.find_patient_context(self.store, "DEMO-U01")
        self.assertEqual((c["encounter"].id, c["bed"].ward, c["bed"].bed, c["active"]), (self.enc, "Ward 4", "12", True))
        self.assertIsNone(ui.find_patient_context(self.store, "DEMO-NOPE"))


class TestCaptureTimeline(HelperBase):
    def test_uses_existing_capture_service_and_is_idempotent(self):
        with mock.patch.object(ui, "capture_typed_ward_round", wraps=ui.capture_typed_ward_round) as spy:
            o1 = self.round()
            o2 = self.round()  # identical submission (e.g. double click) -> same capture id
        self.assertEqual(spy.call_count, 2)
        self.assertTrue(o1.ok and o2.ok)
        self.assertGreater(o1.data["n_appended"], 0)
        self.assertEqual((o2.data["n_appended"], o2.data["n_duplicates"]), (0, o1.data["n_appended"]))
        self.assertEqual(counts(self.store)["captures"], 1)
        self.assertTrue(o1.data["has_medication"])
        med = [i for i in o1.data["items"] if i["category"] == "MEDICATION_ORDER"]
        self.assertTrue(med[0]["value"].startswith("PROPOSED (not prescribed)"))
        self.assertEqual(med[0]["verification"], "unverified")

    def test_capture_errors_are_friendly(self):
        o = self.round(text="   ")
        self.assertFalse(o.ok)
        self.assertNotIn("Traceback", o.message)
        o = self.round(role=Role.WARD_NURSE)
        self.assertFalse(o.ok)
        with mock.patch.object(Timeline, "append_many", side_effect=RuntimeError("db down")):
            o = self.round(text="Patient has fever.")
        self.assertFalse(o.ok)
        self.assertEqual(o.data["status"], "failed")
        self.assertNotIn("Traceback", o.message)
        self.assertIn("Traceback", o.debug)  # only in the developer expander

    def test_timeline_active_vs_full_and_correction(self):
        self.round()
        temp = next(r for r in ui.timeline_view(self.store, self.enc, False)["rows"] if r["type"] == "temperature")
        o = ui.correct_vital(self.store, temp["event_id"], values={"value": 100.4}, unit="F", reason="re-measured",
                             author_id="u_dr_demo", author_role=Role.CONSULTANT)
        self.assertTrue(o.ok)
        active = ui.timeline_view(self.store, self.enc, False)["rows"]
        full = ui.timeline_view(self.store, self.enc, True)["rows"]
        self.assertNotIn(temp["event_id"], [r["event_id"] for r in active])
        self.assertIn(("superseded", "101 F"), [(r["status"], r["value"]) for r in full])
        self.assertIn(("active", "100.4 F"), [(r["status"], r["value"]) for r in active])
        # original row untouched
        self.assertEqual(self.store.get_event(temp["event_id"]).payload["value"], 101)

    def test_conflict_surfaced_not_resolved(self):
        self.round()
        self.round(text="Pulse 118.", who="u_res_demo", role=Role.RESIDENT)  # same time, different value
        tv = ui.timeline_view(self.store, self.enc, False)
        self.assertEqual([(c["category"], c["key"]) for c in tv["conflicts"]], [("VITAL", "pulse")])
        self.assertEqual(sum(1 for r in tv["rows"] if r["type"] == "pulse" and r["status"] == "active"), 2)

    def test_retract_via_timeline(self):
        self.round()
        fever = next(r for r in ui.timeline_view(self.store, self.enc, False)["rows"] if r["type"] == "present")
        with mock.patch.object(Timeline, "retract", wraps=Timeline(self.store).retract) as spy:
            o = ui.retract_event(self.store, fever["event_id"], reason="wrong patient", author_id="u_dr_demo",
                                 author_role=Role.CONSULTANT)
        self.assertTrue(o.ok)
        spy.assert_called_once()


class TestNoteApprovalExport(HelperBase):
    def test_generation_uses_existing_services_and_no_duplicate_document(self):
        self.round()
        with mock.patch.object(ui, "generate_progress_note", wraps=ui.generate_progress_note) as g, \
                mock.patch.object(ui, "create_progress_note_document", wraps=ui.create_progress_note_document) as c:
            o = self.gen()
        self.assertTrue(o.ok)
        g.assert_called_once()
        c.assert_called_once()
        again = self.gen()
        self.assertFalse(again.ok)
        self.assertEqual(counts(self.store)["documents"], 1)

    def test_stale_surfaced_and_regeneration_explicit(self):
        self.round()
        self.gen()
        temp = next(r for r in ui.timeline_view(self.store, self.enc, False)["rows"] if r["type"] == "temperature")
        ui.correct_vital(self.store, temp["event_id"], values={"value": 100.4}, unit="F", reason="re-measured",
                         author_id="u_dr_demo", author_role=Role.CONSULTANT)
        n = ui.current_note(self.store, self.enc)
        self.assertTrue(n["stale"] and n["stale_reasons"])
        self.assertEqual(n["version"].version, 1)  # reading never regenerates
        self.assertEqual(ui.current_note(self.store, self.enc)["version"].version, 1)
        with mock.patch.object(ui, "regenerate_progress_note", wraps=ui.regenerate_progress_note) as spy:
            o = ui.regenerate_note(self.store, n["document"].id, generated_by="u_dr_demo",
                                   generated_role=Role.CONSULTANT, generated_at=T0 + 4 * H)
        self.assertTrue(o.ok)
        spy.assert_called_once()
        n2 = ui.current_note(self.store, self.enc)
        self.assertEqual((n2["version"].version, n2["stale"]), (2, False))

    def test_approval_uses_existing_service_and_surfaces_result(self):
        self.round()
        self.gen()
        doc = ui.current_note(self.store, self.enc)["document"]
        with mock.patch.object(ui, "approve", wraps=ui.approve) as spy:
            bad = ui.approve_note(self.store, doc.id, version=1, approver_id="u_nurse", approver_role=Role.WARD_NURSE,
                                  approved_at=T0 + 4 * H)
            good = ui.approve_note(self.store, doc.id, version=1, approver_id="u_cons_demo",
                                   approver_role=Role.CONSULTANT, approved_at=T0 + 4 * H)
        self.assertEqual(spy.call_count, 2)
        self.assertFalse(bad.ok)
        self.assertTrue(any("cannot approve" in d for d in bad.details))
        self.assertTrue(good.ok)
        self.assertEqual((good.data["approved_by"], good.data["approved_role"], good.data["approved_at"]),
                         ("u_cons_demo", "CONSULTANT", "2026-10-06 12:00"))
        n = ui.current_note(self.store, self.enc)
        self.assertTrue(n["approved"])
        # approval does not verify the underlying events
        evs = [e for e in Timeline(self.store).active_events(self.enc) if e.category != EventCategory.ADT]
        self.assertTrue(all(e.verification.value == "unverified" for e in evs))

    def test_refusal_reasons_red(self):
        o = ui.process_ward_round(self.store, encounter_id=self.enc, text="Pulse 82.", author_id="u_dr_demo",
                                  author_role=Role.CONSULTANT, captured_at=T0 + 2 * H)
        self.assertTrue(o.ok)
        self.gen()
        doc = ui.current_note(self.store, self.enc)["document"]
        r = ui.approve_note(self.store, doc.id, version=1, approver_id="u_cons_demo", approver_role=Role.CONSULTANT)
        self.assertFalse(r.ok)
        self.assertIn("required section S has no supported information", r.details)
        self.assertIn("required section A has no supported information", r.details)

    def test_exports_use_renderer_in_memory(self):
        self.round()
        self.gen()
        doc = ui.current_note(self.store, self.enc)["document"]
        with mock.patch.object(ui, "render_progress_note_html", wraps=ui.render_progress_note_html) as h, \
                mock.patch.object(ui, "render_progress_note_docx", wraps=ui.render_progress_note_docx) as d:
            html = ui.export_html(self.store, doc.id, 1)
            docx = ui.export_docx(self.store, doc.id, 1)
        h.assert_called_once_with(self.store, doc.id, 1)
        self.assertIsInstance(html, bytes)
        self.assertIn(b"DRAFT", html)
        if ui.DOCX_AVAILABLE:
            d.assert_called_once_with(self.store, doc.id, 1)
            self.assertTrue(docx.startswith(b"PK"))

    def test_module_imports_no_llm(self):
        import ast
        with open(ui.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                mods.add(("." * n.level) + (n.module or ""))
        self.assertTrue(mods <= {"hashlib", "os", "re", "decimal", "tempfile", "traceback", "dataclasses", "datetime", "typing",
                                 ".capture", ".documents", ".encounter", ".models", ".progress_note", ".render",
                                 ".store", ".timeline"}, mods)


class TestStreamlitApp(unittest.TestCase):
    """End-to-end AppTest of demo/ipd_app.py against a temp DB outside the repo."""

    def setUp(self):
        trap_llm(self)
        self.tmp = tempfile.mkdtemp(prefix="medibytes_ipd_uitest_")
        self.assertFalse(ui._inside(self.tmp, _REPO))
        self.db = os.path.join(self.tmp, "ipd.sqlite")
        env = mock.patch.dict(os.environ, {"MEDIBYTES_IPD_DB": self.db})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.at = AppTest.from_file(os.path.join(_DEMO, "ipd_app.py"), default_timeout=120)

    def db_counts(self):
        with IpdStore(self.db) as s:
            return counts(s)

    def repo_files(self):
        out = set()
        for base, dirs, files in os.walk(_REPO):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
            out |= {os.path.join(base, f) for f in files}
        return out

    def ok(self):
        self.assertEqual([str(e.value)[:300] for e in self.at.exception], [])

    def admit(self):
        at = self.at
        at.text_input(key="ipd_adm_name").input("Demo Patient S01")
        at.text_input(key="ipd_adm_mrn").input("DEMO-S01")
        at.text_input(key="ipd_adm_ward").input("Ward 4")
        at.text_input(key="ipd_adm_bed").input("12")
        at.text_input(key="ipd_adm_by").input("u_dr_demo")
        at.selectbox(key="ipd_adm_role").select("CONSULTANT")
        at.button(key="ipd_admit").click().run()
        self.ok()

    def test_full_flow_rerun_safety(self):
        files_before = self.repo_files()
        at = self.at
        at.run()
        self.ok()
        self.assertTrue(any("SYNTHETIC" in w.value for w in at.warning))
        self.assertEqual(self.db_counts()["patients"], 0)          # opening the page writes nothing
        self.admit()
        c1 = self.db_counts()
        self.assertEqual((c1["patients"], c1["encounters"]), (1, 1))
        for _ in range(3):                                          # plain reruns: no duplicate admission
            at.run()
        self.assertEqual(self.db_counts(), c1)
        at.button(key="ipd_admit").click().run()                    # clicking admit again: no new encounter
        self.assertEqual(self.db_counts()["encounters"], 1)

        at.text_area(key="ipd_round_text").input(NOTE)
        at.text_input(key="ipd_round_by").input("u_dr_demo")
        at.run()
        at.run()
        self.assertEqual(self.db_counts()["captures"], 0)           # typing + reruns never capture
        at.button(key="ipd_process").click().run()
        self.ok()
        c2 = self.db_counts()
        self.assertEqual(c2["captures"], 1)
        self.assertGreater(c2["events"], c1["events"])
        self.assertTrue(any("PROPOSED" in w.value for w in at.warning))
        at.run()
        self.assertEqual(self.db_counts(), c2)                      # no reprocessing on rerun

        at.text_input(key="ipd_gen_by").input("u_dr_demo")
        at.run()
        self.assertEqual(self.db_counts()["documents"], 0)          # no automatic note
        at.button(key="ipd_generate").click().run()
        self.ok()
        self.assertEqual((self.db_counts()["documents"], self.db_counts()["document_versions"]), (1, 1))
        at.run()
        self.assertEqual(self.db_counts()["document_versions"], 1)
        self.assertTrue(any("DRAFT" in e.value for e in at.error))

        at.text_input(key="ipd_appr_by").input("u_cons_demo")
        at.selectbox(key="ipd_appr_role").select("WARD_NURSE")
        at.button(key="ipd_approve").click().run()
        self.ok()
        self.assertTrue(any("Approval refused" in e.value for e in at.error))
        self.assertTrue(any("cannot approve" in m.value for m in at.markdown))
        at.selectbox(key="ipd_appr_role").select("CONSULTANT")
        at.run()
        with IpdStore(self.db) as s:
            self.assertEqual(s._conn.execute("SELECT COUNT(*) FROM document_versions WHERE approved_at IS NOT NULL")
                             .fetchone()[0], 0)                     # never approved automatically
        at.button(key="ipd_approve").click().run()
        self.ok()
        self.assertTrue(any(s.value.startswith("APPROVED — by u_cons_demo (CONSULTANT)") for s in at.success))
        self.assertEqual(len(at.get("download_button")), 2 if ui.DOCX_AVAILABLE else 1)
        self.assertEqual(self.repo_files(), files_before)          # no exports/DB files in the repo

    def test_stale_surfaced_and_conflict_warning(self):
        at = self.at
        at.run()
        self.admit()
        at.text_area(key="ipd_round_text").input(NOTE)
        at.text_input(key="ipd_round_by").input("u_dr_demo")
        at.button(key="ipd_process").click().run()
        at.text_input(key="ipd_gen_by").input("u_dr_demo")
        at.button(key="ipd_generate").click().run()
        self.ok()
        # conflicting pulse at the same capture time, different author
        at.text_area(key="ipd_round_text").input("Pulse 118.")
        at.text_input(key="ipd_round_by").input("u_res_demo")
        at.selectbox(key="ipd_round_role").select("RESIDENT")
        at.button(key="ipd_process").click().run()
        self.ok()
        self.assertTrue(any("conflict" in e.value.lower() for e in at.error))
        self.assertTrue(any("Progress Note is stale" in e.value for e in at.error))
        v_before = self.db_counts()["document_versions"]
        at.run()
        self.assertEqual(self.db_counts()["document_versions"], v_before)  # never silently regenerated
        at.button(key="ipd_regen").click().run()
        self.ok()
        self.assertEqual(self.db_counts()["document_versions"], v_before + 1)
        self.assertTrue(any("REVIEW REQUIRED" in w.value for w in at.warning))


class TestCorrectionHelperRegression(HelperBase):
    """Manual Step 9 bugs: 0.0 F, lost author_id, 100.79999999999986 F."""

    def setUp(self):
        super().setUp()
        self.round()
        self.temp = next(r for r in ui.timeline_view(self.store, self.enc, False)["rows"] if r["type"] == "temperature")

    def test_decimal_parsing_exact(self):
        for raw, expect in (("100.4", 100.4), ("100.40", 100.4), (" 98.6 ", 98.6), ("101", 101), ("101.0", 101),
                            (100.4, 100.4)):
            got = ui.parse_decimal_input(raw)
            self.assertEqual((got, repr(got)), (expect, repr(expect)))
        for bad in ("", "  ", None, "abc", "-1", "1e2", "100,4", "0.1+0.2", True):
            with self.assertRaises(ui.UiError):
                ui.parse_decimal_input(bad)

    def test_backend_receives_exact_value_and_author(self):
        with mock.patch.object(Timeline, "correct", autospec=True, side_effect=Timeline.correct) as spy:
            o = ui.correct_vital(self.store, self.temp["event_id"], values={"value": "100.4"}, unit="F",
                                 reason="re-measured", author_id="u_dr_demo", author_role=Role.CONSULTANT)
        self.assertTrue(o.ok, o.message)
        kw = spy.call_args.kwargs
        self.assertEqual(kw["payload"], {"value": 100.4, "unit": "F"})
        self.assertEqual(repr(kw["payload"]["value"]), "100.4")
        self.assertEqual(kw["author_id"], "u_dr_demo")
        stored = self.store.get_event(o.data["event_id"])
        self.assertEqual(stored.payload["value"], 100.4)
        self.assertNotEqual(stored.payload["value"], 100.79999999999986)
        self.assertEqual(stored.author_id, "u_dr_demo")
        self.assertEqual(o.message.split(".")[0] + "." + o.message.split(".")[1], "Correction recorded: 100.4 F")

    def test_empty_value_is_refused_not_zero(self):
        before = counts(self.store)["events"]
        for raw in ("", "   "):
            o = ui.correct_vital(self.store, self.temp["event_id"], values={"value": raw}, unit="F",
                                 reason="re-measured", author_id="u_dr_demo", author_role=Role.CONSULTANT)
            self.assertFalse(o.ok)
            self.assertIn("value is required", o.message)
        self.assertEqual(counts(self.store)["events"], before)
        self.assertEqual(Timeline(self.store).status_of(self.temp["event_id"]), "active")

    def test_blank_author_refused(self):
        o = ui.correct_vital(self.store, self.temp["event_id"], values={"value": "100.4"}, unit="F",
                             reason="re-measured", author_id="  ", author_role=Role.CONSULTANT)
        self.assertFalse(o.ok)
        self.assertEqual(Timeline(self.store).status_of(self.temp["event_id"]), "active")


class TestCorrectionFormRegression(unittest.TestCase):
    """AppTest of the real correction form in demo/ipd_app.py (temp DB outside the repo)."""

    def setUp(self):
        trap_llm(self)
        self.tmp = tempfile.mkdtemp(prefix="medibytes_ipd_fixtest_")
        self.db = os.path.join(self.tmp, "ipd.sqlite")
        env = mock.patch.dict(os.environ, {"MEDIBYTES_IPD_DB": self.db})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        at = self.at = AppTest.from_file(os.path.join(_DEMO, "ipd_app.py"), default_timeout=120)
        at.run()
        for k, v in (("ipd_adm_name", "Demo Patient F01"), ("ipd_adm_mrn", "DEMO-F01"), ("ipd_adm_ward", "Ward 4"),
                     ("ipd_adm_bed", "12"), ("ipd_adm_by", "u_dr_demo")):
            at.text_input(key=k).input(v)
        at.button(key="ipd_admit").click().run()
        at.text_area(key="ipd_round_text").input(NOTE)
        at.text_input(key="ipd_round_by").input("u_dr_demo")
        at.button(key="ipd_process").click().run()
        self.ok()
        with IpdStore(self.db) as s:
            enc = s.get_patient_by_mrn("DEMO-F01")
            self.enc = s.list_encounters(enc.id)[0].id
            self.temp_id = next(e.id for e in Timeline(s).active_events(self.enc) if e.subtype == "temperature")

    def ok(self):
        self.assertEqual([str(e.value)[:300] for e in self.at.exception], [])

    def events(self, full=True):
        with IpdStore(self.db) as s:
            return Timeline(s).history(self.enc, active_only=not full)

    def fill(self, n=0, value="100.4", by="u_dr_demo", reason="re-measured", unit="F"):
        at = self.at
        at.selectbox(key="ipd_fix_event").set_value(self.temp_id)
        at.run()
        at.text_input(key=f"ipd_fix_reason_{n}").input(reason)
        at.text_input(key=f"ipd_fix_by_{n}").input(by)
        at.selectbox(key=f"ipd_fix_role_{n}").select("CONSULTANT")
        at.text_input(key=f"ipd_fix_val_{n}").input(value)
        at.text_input(key=f"ipd_fix_unit_{n}").input(unit)

    def submit(self, n=0):
        next(b for b in self.at.button if b.label == "Record correction" and not b.disabled).click().run()
        self.ok()

    def test_no_correction_from_reruns_and_values_survive(self):
        n_before = len(self.events())
        self.fill()
        for _ in range(3):  # normal reruns (e.g. toggling history, selecting tabs)
            self.at.run()
            self.ok()
        self.assertEqual(len(self.events()), n_before)  # nothing written without the button
        at = self.at
        self.assertEqual(at.selectbox(key="ipd_fix_event").value, self.temp_id)
        self.assertEqual((at.text_input(key="ipd_fix_reason_0").value, at.text_input(key="ipd_fix_by_0").value,
                          at.selectbox(key="ipd_fix_role_0").value, at.text_input(key="ipd_fix_val_0").value,
                          at.text_input(key="ipd_fix_unit_0").value),
                         ("re-measured", "u_dr_demo", "CONSULTANT", "100.4", "F"))
        self.at.toggle(key="ipd_full_history").set_value(True).run()
        self.assertEqual(self.at.text_input(key="ipd_fix_val_0").value, "100.4")

    def test_correction_recorded_exactly_once_with_exact_value_and_author(self):
        n_before = len(self.events())
        self.fill()
        self.at.run()                      # a rerun before submitting must not lose anything
        with mock.patch.object(Timeline, "correct", autospec=True, side_effect=Timeline.correct) as spy:
            self.submit()
        self.assertEqual(spy.call_count, 1)
        kw = spy.call_args.kwargs
        self.assertEqual((kw["payload"], kw["author_id"], kw["reason"]),
                         ({"value": 100.4, "unit": "F"}, "u_dr_demo", "re-measured"))
        full, active = self.events(), self.events(full=False)
        self.assertEqual(len(full), n_before + 1)                     # exactly one new event
        new = [e for e in full if e.supersedes_event_id == self.temp_id]
        self.assertEqual(len(new), 1)
        new = new[0]
        self.assertEqual((new.payload["value"], repr(new.payload["value"]), new.payload["unit"]), (100.4, "100.4", "F"))
        self.assertNotEqual(new.payload["value"], 0.0)
        self.assertNotEqual(new.payload["value"], 100.79999999999986)
        self.assertEqual(new.author_id, "u_dr_demo")
        with IpdStore(self.db) as s:
            tl = Timeline(s)
            self.assertEqual((tl.status_of(self.temp_id), tl.status_of(new.id)), ("superseded", "active"))
            self.assertEqual(s.get_event(self.temp_id).payload["value"], 101)   # original untouched
        self.assertIn(self.temp_id, [e.id for e in full])
        self.assertNotIn(self.temp_id, [e.id for e in active])
        self.assertIn(new.id, [e.id for e in active])
        self.assertTrue(any(s.value.startswith("Correction recorded: 100.4 F") for s in self.at.success))
        # form reset only after success: the corrected (active) event gets a fresh, empty form
        self.at.selectbox(key="ipd_fix_event").set_value(new.id).run()
        self.assertEqual(self.at.text_input(key="ipd_fix_val_1").value, "")
        self.assertEqual(self.at.text_input(key="ipd_fix_by_1").value, "")
        self.assertEqual(self.at.text_input(key="ipd_fix_reason_1").value, "")
        self.at.run()
        self.assertEqual(len(self.events()), n_before + 1)

    def test_failed_correction_keeps_the_form(self):
        n_before = len(self.events())
        self.fill(by="")                   # clinician ID missing
        self.submit()
        self.assertTrue(any("Correction refused" in e.value for e in self.at.error))
        self.assertEqual(len(self.events()), n_before)
        self.assertEqual(self.at.text_input(key="ipd_fix_val_0").value, "100.4")   # not reset on failure
        self.at.text_input(key="ipd_fix_by_0").input("u_dr_demo")
        self.submit()
        self.assertEqual(len(self.events()), n_before + 1)

    def test_empty_value_never_becomes_zero(self):
        n_before = len(self.events())
        self.fill(value="")
        self.submit()
        self.assertTrue(any("value is required" in e.value for e in self.at.error))
        self.assertEqual(len(self.events()), n_before)
        self.assertFalse(any(e.payload.get("value") == 0.0 for e in self.events() if e.category == EventCategory.VITAL))


class TestClinicianIdFields(unittest.TestCase):
    """Final cleanup: ID fields (A, B, D, E) have no look-alike placeholder; empty IDs are refused."""

    ID_KEYS = ("ipd_adm_by", "ipd_round_by", "ipd_gen_by", "ipd_appr_by")

    def setUp(self):
        trap_llm(self)
        self.tmp = tempfile.mkdtemp(prefix="medibytes_ipd_idtest_")
        self.db = os.path.join(self.tmp, "ipd.sqlite")
        env = mock.patch.dict(os.environ, {"MEDIBYTES_IPD_DB": self.db})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.at = AppTest.from_file(os.path.join(_DEMO, "ipd_app.py"), default_timeout=120)
        self.at.run()

    def ok(self):
        self.assertEqual([str(e.value)[:300] for e in self.at.exception], [])

    def db_counts(self):
        with IpdStore(self.db) as s:
            c = counts(s)
            c["approved"] = s._conn.execute(
                "SELECT COUNT(*) FROM document_versions WHERE approved_at IS NOT NULL").fetchone()[0]
            return c

    def reruns_write_nothing(self):
        before = self.db_counts()
        for _ in range(2):
            self.at.run()
            self.ok()
        self.assertEqual(self.db_counts(), before)

    def errors(self):
        return [e.value for e in self.at.error]

    def admit(self, by):
        at = self.at
        for k, v in (("ipd_adm_name", "Demo Patient I01"), ("ipd_adm_mrn", "DEMO-I01"), ("ipd_adm_ward", "Ward 4"),
                     ("ipd_adm_bed", "12"), ("ipd_adm_by", by)):
            at.text_input(key=k).input(v)
        at.button(key="ipd_admit").click().run()
        self.ok()

    def round(self, by):
        self.at.text_area(key="ipd_round_text").input(NOTE)
        self.at.text_input(key="ipd_round_by").input(by)
        self.at.button(key="ipd_process").click().run()
        self.ok()

    def test_no_placeholder_on_any_id_field(self):
        self.admit("u_dr_real_01")
        self.round("u_dr_real_01")
        self.at.text_input(key="ipd_gen_by").input("u_dr_real_01")
        self.at.button(key="ipd_generate").click().run()
        for k in self.ID_KEYS:
            w = self.at.text_input(key=k)
            self.assertEqual(w.proto.placeholder, "", k)
            self.assertIn("(required)", w.label)
        self.assertEqual(self.at.text_input(key="ipd_appr_by").value, "")  # nothing pre-filled

    def test_admission_empty_id_refused_and_entered_id_unchanged(self):
        self.admit("")
        self.assertTrue(any("Admission refused" in e for e in self.errors()))
        self.assertEqual(self.db_counts()["patients"], 0)
        self.reruns_write_nothing()
        with mock.patch.object(ui, "admit_patient", wraps=ui.admit_patient) as spy:
            self.at.text_input(key="ipd_adm_by").input("u_dr_real_01")
            self.at.button(key="ipd_admit").click().run()
        self.assertEqual(spy.call_args.kwargs["admitted_by"], "u_dr_real_01")
        with IpdStore(self.db) as s:
            enc = s.list_encounters(s.get_patient_by_mrn("DEMO-I01").id)[0]
            adt = s.list_events(enc.id)[0]
        self.assertEqual(adt.author_id, "u_dr_real_01")
        self.reruns_write_nothing()

    def test_ward_round_empty_id_refused_and_entered_id_unchanged(self):
        self.admit("u_dr_real_01")
        self.round("")
        self.assertTrue(any("author_id is required" in e for e in self.errors()))
        self.assertEqual(self.db_counts()["captures"], 0)
        self.reruns_write_nothing()
        with mock.patch.object(ui, "capture_typed_ward_round", wraps=ui.capture_typed_ward_round) as spy:
            self.round("u_dr_real_02")
        self.assertEqual(spy.call_args.kwargs["author_id"], "u_dr_real_02")
        with IpdStore(self.db) as s:
            caps = s._conn.execute("SELECT author_id FROM captures").fetchall()
        self.assertEqual([c[0] for c in caps], ["u_dr_real_02"])
        self.reruns_write_nothing()

    def test_note_generation_empty_id_refused_and_entered_id_unchanged(self):
        self.admit("u_dr_real_01")
        self.round("u_dr_real_01")
        self.at.button(key="ipd_generate").click().run()
        self.ok()
        self.assertTrue(any("generated_by is required" in e for e in self.errors()))
        self.assertEqual(self.db_counts()["documents"], 0)
        self.reruns_write_nothing()
        with mock.patch.object(ui, "generate_progress_note", wraps=ui.generate_progress_note) as g, \
                mock.patch.object(ui, "create_progress_note_document", wraps=ui.create_progress_note_document) as c:
            self.at.text_input(key="ipd_gen_by").input("u_dr_real_03")
            self.at.button(key="ipd_generate").click().run()
        self.assertEqual(g.call_args.kwargs["generated_by"], "u_dr_real_03")
        self.assertEqual(c.call_args.kwargs["created_by"], "u_dr_real_03")
        with IpdStore(self.db) as s:
            self.assertEqual(s._conn.execute("SELECT created_by FROM document_versions").fetchone()[0], "u_dr_real_03")
        self.reruns_write_nothing()

    def test_approval_empty_id_refused_and_entered_id_unchanged(self):
        self.admit("u_dr_real_01")
        self.round("u_dr_real_01")
        self.at.text_input(key="ipd_gen_by").input("u_dr_real_01")
        self.at.button(key="ipd_generate").click().run()
        self.at.selectbox(key="ipd_appr_role").select("CONSULTANT")
        self.at.button(key="ipd_approve").click().run()
        self.ok()
        self.assertTrue(any("Approval refused" in e for e in self.errors()))
        self.assertEqual(self.db_counts()["approved"], 0)
        self.reruns_write_nothing()
        with mock.patch.object(ui, "approve", wraps=ui.approve) as spy:
            self.at.text_input(key="ipd_appr_by").input("u_cons_real_04")
            self.at.button(key="ipd_approve").click().run()
        self.ok()
        self.assertEqual(spy.call_args.kwargs["approved_by"], "u_cons_real_04")
        with IpdStore(self.db) as s:
            self.assertEqual(s._conn.execute("SELECT approved_by FROM document_versions").fetchone()[0],
                             "u_cons_real_04")
        self.assertTrue(any(x.value.startswith("APPROVED — by u_cons_real_04 (CONSULTANT)") for x in self.at.success))
        self.reruns_write_nothing()


class TestErRegression(unittest.TestCase):
    def test_er_extraction_unchanged(self):
        h = lambda o: hashlib.sha256(json.dumps(o, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]  # noqa: E731
        got = {}
        for job in ("demo-001", "demo-002"):
            tj, ej = stt_extract.run_stt_extract(os.path.join(_DEMO, "cleaned", f"{job}.wav"), job, use_llm=False,
                                                 model="mock")
            got[job] = (h(tj), h(ej))
        self.assertEqual(got, {"demo-001": ("43c32d9c00ada83f", "5dcdbfa65b309640"),
                               "demo-002": ("496c0d18c80a7ad2", "89d94317c2ae36a8")})


if __name__ == "__main__":
    unittest.main()
