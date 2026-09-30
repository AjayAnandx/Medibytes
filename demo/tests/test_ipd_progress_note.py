"""IPD P1 step 6: deterministic SOAP progress-note draft (read-only over the timeline).

Run:  python -m unittest demo/tests/test_ipd_progress_note.py -v   (from repo root)
In-memory SQLite, synthetic data, no LLM.
"""
import os

import sys
import unittest
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO = os.path.dirname(_HERE)
sys.path.insert(0, _DEMO)

from ipd import progress_note as pn_mod  # noqa: E402
from ipd.capture import capture_typed_ward_round  # noqa: E402
from ipd.encounter import admit_patient, synthetic_patient  # noqa: E402
from ipd.models import (  # noqa: E402
    IST, DocumentStatus, Event, EventCategory, Role, SourceType, Verification,
)
from ipd.progress_note import (  # noqa: E402
    CLINICAL, NIL, PLACEHOLDER, RED, YELLOW, GREEN, ProgressNoteError, generate_progress_note,
)
from ipd.store import IpdStore  # noqa: E402
from ipd.timeline import Timeline  # noqa: E402

T0 = datetime(2026, 10, 6, 8, 0, tzinfo=IST)
H = timedelta(hours=1)
GEN_AT = T0 + 12 * H


class NoteBase(unittest.TestCase):
    def setUp(self):
        self.store = IpdStore()
        self.tl = Timeline(self.store)
        self.enc = admit_patient(self.store, synthetic_patient("N01"), ward="Ward 4", bed="12",
                                 admitted_by="u_dr_demo", admitted_role=Role.CONSULTANT, admit_at=T0).encounter.id

    def tearDown(self):
        self.store.close()

    def ev(self, category, subtype, payload, *, at=None, enc=None, verification=Verification.UNVERIFIED, **kw):
        e = Event(encounter_id=enc or self.enc, occurred_at=at or T0 + 2 * H, category=category, subtype=subtype,
                  payload=payload, author_id="u_dr_demo", author_role=Role.CONSULTANT,
                  source_type=SourceType.TYPED, confidence=0.9, verification=verification, **kw)
        self.tl.append(e)
        return e

    def sym(self, term, denied=False, **kw):
        return self.ev(EventCategory.SYMPTOM, "denied" if denied else "present", {"term": term, "negated": denied}, **kw)

    def vital(self, subtype, payload, **kw):
        return self.ev(EventCategory.VITAL, subtype, payload, **kw)

    def note(self, enc=None, frm=T0, to=T0 + 24 * H, by="u_dr_demo", at=GEN_AT):
        return generate_progress_note(self.tl, enc or self.enc, window_from=frm, window_to=to,
                                      generated_by=by, generated_at=at)

    def texts(self, note, key):
        return [l.text for l in note.section(key).lines]


class TestSections(NoteBase):
    def test_subjective_present_and_denied(self):
        f, c = self.sym("fever"), self.sym("chest pain", denied=True)
        s = self.note().section("S")
        self.assertEqual([(l.text, l.event_ids, l.kind) for l in s.lines],
                         [("Fever present.", (f.id,), CLINICAL), ("Chest pain denied.", (c.id,), CLINICAL)])

    def test_objective_vitals_units_only_when_present(self):
        self.vital("temperature", {"value": 101, "unit": "F"})
        self.vital("temperature", {"value": 101, "unit": None}, at=T0 + 3 * H)
        self.vital("spo2", {"value": 95, "unit": "%"}, at=T0 + 4 * H)
        self.vital("bp", {"systolic": 118, "diastolic": 76}, at=T0 + 5 * H)
        self.vital("pulse", {"value": 82}, at=T0 + 6 * H)
        self.assertEqual(self.texts(self.note(), "O"),
                         ["10:00 Temperature 101 F", "11:00 Temperature 101", "12:00 SpO2 95 %",
                          "13:00 BP 118/76", "14:00 Pulse 82"])  # no guessed F / mmHg / bpm

    def test_multiple_vitals_chronological(self):
        late = self.vital("spo2", {"value": 97, "unit": "%"}, at=T0 + 6 * H)
        early = self.vital("spo2", {"value": 92, "unit": "%"}, at=T0 + 2 * H)
        mid = self.vital("spo2", {"value": 95, "unit": "%"}, at=T0 + 4 * H)
        o = self.note().section("O")
        self.assertEqual([l.event_ids for l in o.lines], [(early.id,), (mid.id,), (late.id,)])

    def test_multi_day_window_shows_dates(self):
        self.vital("pulse", {"value": 80}, at=T0 + 2 * H)
        self.vital("pulse", {"value": 90}, at=T0 + 26 * H)
        self.assertEqual(self.texts(self.note(to=T0 + 48 * H), "O"), ["06-Oct 10:00 Pulse 80", "07-Oct 10:00 Pulse 90"])

    def test_assessment_provisional_stays_provisional(self):
        d = self.ev(EventCategory.DIAGNOSIS, "provisional", {"text": "viral fever"})
        a = self.note().section("A")
        self.assertEqual([(l.text, l.event_ids) for l in a.lines], [("Provisional diagnosis: viral fever.", (d.id,))])
        self.assertNotIn("Diagnosis: viral fever", a.lines[0].text.replace("Provisional diagnosis", ""))

    def test_plan_followup_and_proposed_medication(self):
        p = self.ev(EventCategory.PLAN, "followup", {"text": "Review after 3 days"})
        m = self.ev(EventCategory.MEDICATION_ORDER, "proposed",
                    {"name": "paracetamol", "dose": 650.0, "unit": "mg", "missing": ["frequency", "duration"]})
        self.assertEqual([(l.text, l.event_ids) for l in self.note().section("P").lines], [
            ("Follow-up: Review after 3 days.", (p.id,)),
            ("Proposed medication (not confirmed, not prescribed): paracetamol 650 mg "
             "(not stated: frequency, duration).", (m.id,))])

    def test_missing_medication_fields_not_invented(self):
        self.ev(EventCategory.MEDICATION_ORDER, "proposed", {"name": "ceftriaxone", "missing": ["dose", "unit",
                                                                                               "frequency", "duration"]})
        (line,) = self.note().section("P").lines
        self.assertEqual(line.text, "Proposed medication (not confirmed, not prescribed): ceftriaxone "
                                    "(not stated: dose, unit, frequency, duration).")
        for word in ("IV", "oral", "twice", "daily", "days", "route"):
            self.assertNotIn(word, line.text)

    def test_proposed_medication_never_active(self):
        self.ev(EventCategory.MEDICATION_ORDER, "proposed", {"name": "paracetamol", "dose": 650, "unit": "mg",
                                                            "frequency": "TDS"})
        note = self.note()
        for l in note.section("P").lines:
            self.assertTrue(l.text.startswith("Proposed medication (not confirmed, not prescribed)"))
            low = l.text.lower().replace("not prescribed", "")
            for bad in ("prescribed", "administered", "given", "active", "started", "rx:"):
                self.assertNotIn(bad, low)
        self.assertEqual(self.store.list_events(self.enc, categories=["MEDICATION_ORDER"])[0].subtype, "proposed")

    def test_non_soap_categories_ignored(self):
        self.ev(EventCategory.ALLERGY, "denied", {"substance": "penicillin", "negated": True})
        note = self.note()
        self.assertEqual(note.source_event_ids, ())  # ADT + ALLERGY not in the P1 note
        self.assertEqual(note.omitted, ())


class TestProvenance(NoteBase):
    def test_every_clinical_line_has_valid_provenance(self):
        ids = {self.sym("fever").id, self.sym("cough").id, self.vital("pulse", {"value": 88}).id,
               self.ev(EventCategory.DIAGNOSIS, "provisional", {"text": "viral fever"}).id,
               self.ev(EventCategory.PLAN, "followup", {"text": "Review tomorrow"}).id}
        note = self.note()
        for s in note.sections:
            for l in s.lines:
                if l.kind == CLINICAL:
                    self.assertTrue(l.event_ids)
                    for eid in l.event_ids:
                        self.assertIn(eid, ids)
                        self.assertEqual(self.tl.status_of(eid), "active")
        self.assertEqual(set(note.source_event_ids), ids)

    def test_identical_statements_merge_with_all_sources(self):
        a, b = self.sym("fever", at=T0 + 2 * H), self.sym("fever", at=T0 + 5 * H)
        (line,) = self.note().section("S").lines
        self.assertEqual((line.text, line.event_ids), ("Fever present.", (a.id, b.id)))

    def test_unrenderable_event_omitted_not_invented(self):
        e = self.vital("temperature", {"unit": "F"})  # no value
        note = self.note()
        self.assertEqual(note.section("O").lines[0].kind, PLACEHOLDER)
        self.assertEqual(note.omitted[0]["event_id"], e.id)

    def test_verification_colour(self):
        self.sym("fever", verification=Verification.VERIFIED)
        self.sym("cough")
        colors = [l.color for l in self.note().section("S").lines]
        self.assertEqual(colors, [GREEN, YELLOW])


class TestActiveView(NoteBase):
    def test_superseded_not_used_and_correction_used(self):
        e1 = self.vital("temperature", {"value": 101, "unit": "F"})
        e2 = self.tl.correct(e1.id, payload={"value": 100.1, "unit": "F"}, reason="misread",
                             author_id="u_dr_demo", author_role=Role.CONSULTANT)
        (line,) = self.note().section("O").lines
        self.assertEqual((line.text, line.event_ids, line.color), ("10:00 Temperature 100.1 F", (e2.id,), GREEN))
        self.assertNotIn(e1.id, self.note().source_event_ids)

    def test_retracted_not_used(self):
        e = self.sym("fever")
        self.tl.retract(e.id, reason="wrong patient", author_id="u_dr_demo", author_role=Role.CONSULTANT)
        s = self.note().section("S")
        self.assertEqual((s.lines[0].kind, s.lines[0].color), (PLACEHOLDER, RED))
        self.assertIn(e.id, [x.id for x in self.tl.history(self.enc)])  # still in full history


class TestPlaceholders(NoteBase):
    def test_empty_sections(self):
        note = self.note()
        expect = {"S": (RED, "No subjective information captured."), "O": (NIL, "No objective findings captured."),
                  "A": (RED, "No assessment information captured."), "P": (NIL, "No plan captured.")}
        for key, (color, text) in expect.items():
            (line,) = note.section(key).lines
            self.assertEqual((line.kind, line.color, line.text, line.event_ids), (PLACEHOLDER, color, text, ()))
        self.assertEqual(note.red_sections, ("S", "A"))
        self.assertEqual(note.source_event_ids, ())
        self.assertEqual([s.required for s in note.sections], [True, False, True, False])

    def test_filled_sections_have_no_placeholders(self):
        self.sym("fever")
        self.ev(EventCategory.DIAGNOSIS, "provisional", {"text": "viral fever"})
        note = self.note()
        self.assertEqual(note.red_sections, ())
        for key in ("S", "A"):
            self.assertTrue(all(l.kind == CLINICAL for l in note.section(key).lines))


class TestConflicts(NoteBase):
    def test_conflicting_vitals_preserved_flagged_not_resolved(self):
        a = self.vital("spo2", {"value": 95, "unit": "%"})
        b = Event(encounter_id=self.enc, occurred_at=T0 + 2 * H, category=EventCategory.VITAL, subtype="spo2",
                  payload={"value": 89, "unit": "%"}, author_id="dev_monitor", author_role=Role.DEVICE,
                  source_type=SourceType.DEVICE, confidence=1.0, verification=Verification.AUTO)
        self.tl.append(b)
        note = self.note()
        o = note.section("O")
        self.assertEqual([(l.text, l.event_ids) for l in o.lines],
                         [("10:00 SpO2 95 %", (a.id,)), ("10:00 SpO2 89 %", (b.id,))])
        self.assertTrue(all(l.needs_review and "review" in l.review_reason for l in o.lines))
        self.assertTrue(o.needs_review and note.needs_review)
        self.assertEqual(len(note.conflicts), 1)
        self.assertEqual(set(note.conflicts[0].event_ids), {a.id, b.id})
        self.assertEqual((self.tl.status_of(a.id), self.tl.status_of(b.id)), ("active", "active"))

    def test_symptom_conflict_both_kept(self):
        self.sym("fever")
        self.sym("fever", denied=True)
        s = self.note().section("S")
        self.assertEqual(self.texts(self.note(), "S"), ["Fever present.", "Fever denied."])
        self.assertTrue(all(l.needs_review for l in s.lines))

    def test_non_conflicting_lines_not_flagged(self):
        self.vital("spo2", {"value": 95, "unit": "%"})
        self.vital("spo2", {"value": 89, "unit": "%"}, at=T0 + 3 * H)  # different time: a trend
        self.assertFalse(self.note().needs_review)


class TestWindowIsolationDeterminism(NoteBase):
    def test_encounter_isolation(self):
        other = admit_patient(self.store, synthetic_patient("N02"), ward="Ward 5", bed="1",
                              admitted_by="u_dr_demo", admitted_role=Role.CONSULTANT, admit_at=T0).encounter.id
        mine, theirs = self.sym("fever"), self.sym("cough", enc=other)
        self.assertEqual(self.note().source_event_ids, (mine.id,))
        self.assertEqual(self.note(enc=other).source_event_ids, (theirs.id,))

    def test_window_bounds(self):
        before = self.sym("headache", at=T0 + 1 * H)
        inside = self.sym("fever", at=T0 + 3 * H)
        at_end = self.sym("cough", at=T0 + 6 * H)
        note = self.note(frm=T0 + 3 * H, to=T0 + 6 * H)  # [from, to)
        self.assertEqual(note.source_event_ids, (inside.id,))
        self.assertNotIn(before.id, note.source_event_ids)
        self.assertNotIn(at_end.id, note.source_event_ids)
        self.assertEqual(self.note(frm=T0 + 1 * H, to=T0 + 2 * H).source_event_ids, (before.id,))

    def test_invalid_inputs(self):
        with self.assertRaises(ProgressNoteError):
            self.note(frm=T0 + 5 * H, to=T0 + 5 * H)
        with self.assertRaises(ProgressNoteError):
            self.note(by="")
        with self.assertRaises(ProgressNoteError):
            self.note(frm=datetime(2026, 10, 6, 8, 0))
        with self.assertRaises(ValueError):
            self.note(enc="enc_missing")

    def test_deterministic(self):
        self.sym("fever")
        self.vital("pulse", {"value": 88})
        self.ev(EventCategory.DIAGNOSIS, "provisional", {"text": "viral fever"})
        a, b = self.note(), self.note()
        self.assertEqual(a, b)
        self.assertEqual(a.to_content(), b.to_content())
        self.assertEqual(a.status, DocumentStatus.DRAFT)
        self.assertEqual((a.generated_by, a.generated_at, a.window_from), ("u_dr_demo", GEN_AT, T0))

    def test_generator_performs_no_writes(self):
        self.sym("fever")
        self.vital("spo2", {"value": 95, "unit": "%"})
        tables = ("patients", "encounters", "bed_assignments", "captures", "source_records", "events",
                  "documents", "document_versions", "audit_log")
        dump = lambda: {t: self.store._conn.execute(f"SELECT * FROM {t}").fetchall() for t in tables}  # noqa: E731
        before = {t: [tuple(r) for r in rows] for t, rows in dump().items()}
        self.note()
        self.assertEqual({t: [tuple(r) for r in rows] for t, rows in dump().items()}, before)

    def test_module_has_no_llm_or_writes(self):
        """Inspect the code (not docstrings): only safe imports, no write calls on timeline/store."""
        import ast
        with open(pn_mod.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imports.add(("." * node.level) + (node.module or ""))
        self.assertEqual(imports, {"dataclasses", "datetime", "typing", ".models", ".timeline"})
        calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and isinstance(n.func.value, ast.Name) and n.func.value.id == "timeline"}
        self.assertEqual(calls, {"active_events", "detect_conflicts"})  # read-only timeline API only
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for banned in ("store", "append_many", "append_event", "correct", "retract", "append_audit"):
            self.assertNotIn(banned, attrs)
        self.assertFalse({a for a in attrs if a.startswith(("add_", "set_", "update_", "mark_", "record_"))})


class TestFromTypedCapture(NoteBase):
    def test_end_to_end_from_ward_round(self):
        text = ("Patient has fever. No chest pain.\nTemperature: 101 F\nDiagnosis: viral fever.\n"
                "Start paracetamol 650 mg.")
        r = capture_typed_ward_round(self.store, encounter_id=self.enc, text=text, author_id="u_dr_demo",
                                     author_role=Role.CONSULTANT, location={"ward": "Ward 4", "bed": "12"},
                                     captured_at=T0 + 2 * H)
        note = self.note()
        self.assertEqual(self.texts(note, "S"), ["Fever present.", "Chest pain denied."])
        self.assertEqual(self.texts(note, "O"), ["10:00 Temperature 101 F"])
        self.assertEqual(self.texts(note, "A"), ["Provisional diagnosis: viral fever."])
        self.assertEqual(self.texts(note, "P"), ["Proposed medication (not confirmed, not prescribed): paracetamol "
                                                 "650 mg (not stated: frequency, duration)."])
        self.assertEqual(set(note.source_event_ids), set(r.appended_event_ids))
        self.assertEqual(note.red_sections, ())


if __name__ == "__main__":
    unittest.main()
