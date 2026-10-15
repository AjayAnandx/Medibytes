"""IPD P1 step 7: progress-note documents — versions, staleness, wording edits, approval.

Run:  python -m unittest demo/tests/test_ipd_documents.py -v   (from repo root)
In-memory SQLite, synthetic data, no LLM.
"""
import copy
import hashlib
import json
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO = os.path.dirname(_HERE)
sys.path.insert(0, _DEMO)

from ipd import documents as docs_mod  # noqa: E402
from ipd.documents import (  # noqa: E402
    APPROVER_ROLES, NOTE_CATEGORIES, ApprovalError, DocumentError, approve, check_approval,
    create_progress_note_document, edit_wording, mark_stale_if_needed, regenerate_progress_note, staleness,
)
from ipd.encounter import admit_patient, synthetic_patient  # noqa: E402
from ipd.models import (  # noqa: E402
    IST, AuditAction, ChangeType, DocumentStatus, DocumentVersion, EncounterStatus, Event, EventCategory, Role,
    SourceType, Verification,
)
from ipd.progress_note import generate_progress_note  # noqa: E402
from ipd.store import IpdStore  # noqa: E402
from ipd.timeline import Timeline  # noqa: E402

T0 = datetime(2026, 10, 6, 8, 0, tzinfo=IST)
H = timedelta(hours=1)
WIN_TO = T0 + 12 * H
DR = ("u_dr_demo", Role.CONSULTANT)


class DocBase(unittest.TestCase):
    def setUp(self):
        self.store = IpdStore()
        self.tl = Timeline(self.store)
        self.enc = self.admit("D01")

    def tearDown(self):
        self.store.close()

    def admit(self, label):
        return admit_patient(self.store, synthetic_patient(label), ward="Ward 4", bed=label,
                             admitted_by=DR[0], admitted_role=DR[1], admit_at=T0).encounter.id

    def ev(self, category, subtype, payload, *, at=None, enc=None, **kw):
        e = Event(encounter_id=enc or self.enc, occurred_at=at or T0 + 2 * H, category=category, subtype=subtype,
                  payload=payload, author_id=DR[0], author_role=DR[1], source_type=SourceType.TYPED,
                  confidence=0.9, **kw)
        self.tl.append(e)
        return e

    def full_timeline(self, enc=None):
        """S, O, A, P all supported."""
        return {"fever": self.ev(EventCategory.SYMPTOM, "present", {"term": "fever", "negated": False}, enc=enc),
                "pulse": self.ev(EventCategory.VITAL, "pulse", {"value": 88}, enc=enc),
                "temp": self.ev(EventCategory.VITAL, "temperature", {"value": 101, "unit": "F"}, enc=enc),
                "dx": self.ev(EventCategory.DIAGNOSIS, "provisional", {"text": "viral fever"}, enc=enc),
                "med": self.ev(EventCategory.MEDICATION_ORDER, "proposed",
                               {"name": "paracetamol", "dose": 650.0, "unit": "mg", "missing": ["frequency", "duration"]},
                               enc=enc)}

    def draft(self, enc=None, to=WIN_TO, at=T0 + 3 * H):
        return generate_progress_note(self.tl, enc or self.enc, window_from=T0, window_to=to,
                                      generated_by=DR[0], generated_at=at)

    def create(self, enc=None):
        return create_progress_note_document(self.store, self.draft(enc), created_by=DR[0], created_role=DR[1])

    def approve(self, doc, version, role=Role.CONSULTANT, who="u_dr_demo"):
        return approve(self.store, doc.id, version=version, approved_by=who, approved_role=role,
                       approved_at=T0 + 4 * H)

    def line_index(self, version, key, startswith):
        sec = next(s for s in version.content["sections"] if s["key"] == key)
        return next(i for i, l in enumerate(sec["lines"]) if l["text"].startswith(startswith))


class TestCreateAndVersions(DocBase):
    def setUp(self):
        super().setUp()
        self.ev_map = self.full_timeline()
        self.dft = self.draft()
        self.doc, self.v1 = create_progress_note_document(self.store, self.dft, created_by=DR[0], created_role=DR[1])

    def test_create_document_and_v1(self):
        d, v = self.doc, self.v1
        self.assertEqual((d.encounter_id, d.type.value, d.window_from, d.window_to, d.current_version, d.status),
                         (self.enc, "progress_note", T0, WIN_TO, 1, DocumentStatus.DRAFT))
        self.assertEqual((v.version, v.change_type, v.stale, v.created_by, v.created_at, v.generator),
                         (1, ChangeType.GENERATED, False, DR[0], self.dft.generated_at, self.dft.generator))
        self.assertIsNone(v.approved_at)

    def test_source_ids_and_content_preserved(self):
        self.assertEqual(self.v1.source_event_ids, list(self.dft.source_event_ids))
        self.assertEqual(set(self.v1.source_event_ids), {e.id for e in self.ev_map.values()})
        self.assertEqual(self.v1.content, self.dft.to_content())
        self.assertEqual(self.store.get_document_version(self.doc.id, 1), self.v1)

    def test_version_content_immutable(self):
        with self.assertRaises(sqlite3.DatabaseError):
            self.store._conn.execute("UPDATE document_versions SET content = '{}' WHERE document_id = ?", (self.doc.id,))
        with self.assertRaises(sqlite3.DatabaseError):
            self.store._conn.execute("DELETE FROM document_versions WHERE document_id = ?", (self.doc.id,))
        self.assertEqual(self.store.get_document_version(self.doc.id, 1), self.v1)

    def test_regeneration_creates_v2_and_keeps_v1(self):
        self.tl.correct(self.ev_map["temp"].id, payload={"value": 100.4, "unit": "F"}, reason="re-measured",
                        author_id=DR[0], author_role=DR[1])
        doc, v2 = regenerate_progress_note(self.store, self.doc.id, generated_by=DR[0], generated_role=DR[1],
                                           generated_at=T0 + 5 * H)
        self.assertEqual((v2.version, v2.change_type, doc.current_version, doc.status),
                         (2, ChangeType.REGENERATED, 2, DocumentStatus.DRAFT))
        self.assertEqual(self.store.get_document_version(self.doc.id, 1), self.v1)  # unchanged
        self.assertIn("Temperature 100.4 F", json.dumps(v2.content))
        self.assertNotIn(self.ev_map["temp"].id, v2.source_event_ids)
        self.assertEqual([v.version for v in self.store.list_document_versions(self.doc.id)], [1, 2])

    def test_create_is_transactional(self):
        real = self.store.append_audit
        with unittest.mock.patch.object(self.store, "append_audit", side_effect=RuntimeError("audit down")):
            with self.assertRaises(RuntimeError):
                create_progress_note_document(self.store, self.draft(), created_by=DR[0], created_role=DR[1])
        self.assertEqual(len(self.store.list_documents(self.enc)), 1)
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM document_versions").fetchone()[0], 1)
        self.assertTrue(callable(real))

    def test_create_validation(self):
        with self.assertRaises(DocumentError):
            create_progress_note_document(self.store, {"not": "a draft"}, created_by=DR[0], created_role=DR[1])
        with self.assertRaises(DocumentError):
            create_progress_note_document(self.store, self.draft(), created_by=DR[0], created_role=Role.WARD_NURSE)


class TestWordingEdits(DocBase):
    def setUp(self):
        super().setUp()
        self.ev_map = self.full_timeline()
        self.doc, self.v1 = self.create()
        self.s0 = self.line_index(self.v1, "S", "Fever present")
        self.p0 = self.line_index(self.v1, "P", "Proposed medication")
        self.o_pulse = self.line_index(self.v1, "O", "10:00 Pulse")

    def edit(self, edits, base=1):
        return edit_wording(self.store, self.doc.id, base_version=base, edits=edits, edited_by=DR[0],
                            edited_role=DR[1], edited_at=T0 + 3.5 * H, reason="phrasing")

    def test_wording_edit_creates_new_version(self):
        doc, v2 = self.edit({("S", self.s0): "Patient reports fever present."})
        self.assertEqual((v2.version, v2.change_type, doc.current_version), (2, ChangeType.WORDING_EDIT, 2))
        line = v2.content["sections"][0]["lines"][self.s0]
        self.assertEqual((line["text"], line["generated_text"]), ("Patient reports fever present.", "Fever present."))
        self.assertEqual(v2.source_event_ids, self.v1.source_event_ids)
        self.assertEqual(self.store.get_document_version(self.doc.id, 1), self.v1)
        # every non-text field of every line is identical
        strip = lambda c: [{k: v for k, v in l.items() if k not in ("text", "generated_text")}  # noqa: E731
                           for s in c["sections"] for l in s["lines"]]
        self.assertEqual(strip(v2.content), strip(self.v1.content))

    def test_cannot_add_facts_or_remove_provenance_or_change_facts(self):
        bad = {
            ("S", self.s0): "Fever present, chest pain denied.",          # adds a new clinical fact
            ("P", self.p0): "Paracetamol 650 mg.",                        # drops the 'proposed / not prescribed' wording
            ("O", self.o_pulse): "10:00 Pulse 92",                        # changes a value
        }
        for key, text in bad.items():
            with self.subTest(text=text):
                with self.assertRaises(DocumentError):
                    self.edit({key: text})
        for text in ("Fever denied.", "No fever present.", "Fever not present."):  # meaning flip
            with self.assertRaises(DocumentError):
                self.edit({("S", self.s0): text})
        self.assertEqual(len(self.store.list_document_versions(self.doc.id)), 1)

    def test_edit_api_cannot_touch_event_ids_or_placeholders(self):
        with self.assertRaises(TypeError):
            edit_wording(self.store, self.doc.id, base_version=1, edits={}, edited_by=DR[0], edited_role=DR[1],
                         edited_at=T0, event_ids=["evt_new"])
        with self.assertRaises(DocumentError):
            self.edit({("X", 0): "anything"})
        with self.assertRaises(DocumentError):
            self.edit({("S", self.s0): "Fever present."})  # no change
        with self.assertRaises(DocumentError):
            self.edit({("S", self.s0): "Patient reports fever present."}, base=2)  # non-current base

    def test_placeholder_not_editable(self):
        enc2 = self.admit("D02")
        doc, v1 = self.create(enc2)
        with self.assertRaises(DocumentError):
            edit_wording(self.store, doc.id, base_version=1, edits={("S", 0): "Patient has fever."},
                         edited_by=DR[0], edited_role=DR[1], edited_at=T0 + 3 * H)

    def test_stale_version_cannot_be_edited(self):
        self.tl.retract(self.ev_map["pulse"].id, reason="wrong bed", author_id=DR[0], author_role=DR[1])
        with self.assertRaises(DocumentError):
            self.edit({("S", self.s0): "Patient reports fever present."})

    def test_edited_version_can_be_approved(self):
        _, v2 = self.edit({("S", self.s0): "Patient reports fever present."})
        r = self.approve(self.doc, 2)
        self.assertEqual(r.version.approved_by, "u_dr_demo")


class TestStaleness(DocBase):
    def setUp(self):
        super().setUp()
        self.ev_map = self.full_timeline()
        self.doc, self.v1 = self.create()

    def test_fresh_version_not_stale(self):
        self.assertEqual(staleness(self.store, self.doc.id), docs_mod.StaleReport(False, ()))

    def test_correction_makes_stale(self):
        self.tl.correct(self.ev_map["temp"].id, payload={"value": 100.4, "unit": "F"}, reason="re-measured",
                        author_id=DR[0], author_role=DR[1])
        rep = staleness(self.store, self.doc.id)
        self.assertTrue(rep.stale)
        self.assertTrue(any("superseded" in r for r in rep.reasons))
        self.assertTrue(any("new VITAL/temperature" in r for r in rep.reasons))

    def test_retraction_makes_stale(self):
        self.tl.retract(self.ev_map["fever"].id, reason="wrong patient", author_id=DR[0], author_role=DR[1])
        rep = staleness(self.store, self.doc.id)
        self.assertTrue(rep.stale and any("retracted" in r for r in rep.reasons))

    def test_new_event_inside_window_makes_stale(self):
        self.ev(EventCategory.VITAL, "spo2", {"value": 95, "unit": "%"}, at=T0 + 6 * H)
        self.assertTrue(staleness(self.store, self.doc.id).stale)

    def test_unrelated_or_outside_window_not_stale(self):
        self.ev(EventCategory.VITAL, "spo2", {"value": 95, "unit": "%"}, at=WIN_TO + H)     # after window
        self.ev(EventCategory.ALLERGY, "denied", {"substance": "penicillin"}, at=T0 + 5 * H)  # not a note category
        other = self.admit("D03")
        self.ev(EventCategory.SYMPTOM, "present", {"term": "cough"}, enc=other)            # other encounter
        self.assertFalse(staleness(self.store, self.doc.id).stale)

    def test_deterministic_and_read_only_until_marked(self):
        self.tl.retract(self.ev_map["pulse"].id, reason="dup", author_id=DR[0], author_role=DR[1])
        self.assertEqual(staleness(self.store, self.doc.id), staleness(self.store, self.doc.id))
        self.assertFalse(self.store.get_document_version(self.doc.id, 1).stale)  # detection alone writes nothing
        mark_stale_if_needed(self.store, self.doc.id)
        self.assertTrue(self.store.get_document_version(self.doc.id, 1).stale)
        self.assertEqual(self.store.get_document(self.doc.id).current_version, 1)  # not silently regenerated

    def test_note_categories_match_generator(self):
        cats = {EventCategory.SYMPTOM: ("present", {"term": "x"}), EventCategory.VITAL: ("pulse", {"value": 1}),
                EventCategory.DIAGNOSIS: ("provisional", {"text": "x"}), EventCategory.PLAN: ("plan", {"text": "x"}),
                EventCategory.MEDICATION_ORDER: ("proposed", {"name": "x"}),
                EventCategory.ALLERGY: ("active", {"substance": "x"}), EventCategory.TASK: ("created", {"text": "x"}),
                EventCategory.EXAM_FINDING: ("finding", {"text": "x"})}
        enc = self.admit("D09")
        ids = {c: self.ev(c, st, p, enc=enc).id for c, (st, p) in cats.items()}
        d = self.draft(enc)
        used = set(d.source_event_ids) | {o["event_id"] for o in d.omitted}
        self.assertEqual({c for c, i in ids.items() if i in used}, set(NOTE_CATEGORIES))


class TestApproval(DocBase):
    def test_roles(self):
        for role in (Role.CONSULTANT, Role.RESIDENT, Role.INTENSIVIST):
            with self.subTest(role=role):
                enc = self.admit(f"A{role.value[:3]}")
                self.full_timeline(enc)
                doc, _ = self.create(enc)
                r = self.approve(doc, 1, role=role)
                self.assertEqual((r.version.approved_role, r.document.status), (role, DocumentStatus.APPROVED))
        self.assertEqual(APPROVER_ROLES, {Role.CONSULTANT, Role.RESIDENT, Role.INTENSIVIST})

    def test_wrong_role_rejected(self):
        self.full_timeline()
        doc, _ = self.create()
        for role in (Role.WARD_NURSE, Role.ICU_NURSE, Role.ADMIN, Role.DEVICE):
            with self.assertRaises(ApprovalError):
                self.approve(doc, 1, role=role)
        self.assertIsNone(self.store.get_document_version(doc.id, 1).approved_at)

    def test_approval_metadata_audit_and_immutability(self):
        self.full_timeline()
        doc, v1 = self.create()
        r = self.approve(doc, 1)
        v = self.store.get_document_version(doc.id, 1)
        self.assertEqual((v.approved_by, v.approved_role, v.approved_at), ("u_dr_demo", Role.CONSULTANT, T0 + 4 * H))
        self.assertEqual((r.document.status, r.document.current_version), (DocumentStatus.APPROVED, 1))
        self.assertEqual(v.content, v1.content)
        audit = [a for a in self.store.list_audit("document", doc.id) if a.action == AuditAction.APPROVE]
        self.assertEqual(len(audit), 1)
        self.assertEqual((audit[0].user_id, audit[0].role, audit[0].detail["version"], audit[0].detail["encounter_id"]),
                         ("u_dr_demo", Role.CONSULTANT, 1, self.enc))
        with self.assertRaises(sqlite3.DatabaseError):
            self.store._conn.execute("UPDATE document_versions SET content = '{}' WHERE document_id = ?", (doc.id,))
        with self.assertRaises(KeyError):
            self.store.record_version_approval(doc.id, 1, "u_other", Role.RESIDENT, T0 + 5 * H)
        again = self.approve(doc, 1, who="u_other")  # idempotent: nothing new written
        self.assertTrue(again.already_approved)
        self.assertEqual(self.store.get_document_version(doc.id, 1).approved_by, "u_dr_demo")
        self.assertEqual(len([a for a in self.store.list_audit("document", doc.id) if a.action == AuditAction.APPROVE]), 1)

    def test_stale_version_cannot_be_approved(self):
        ev = self.full_timeline()
        doc, _ = self.create()
        self.tl.correct(ev["pulse"].id, payload={"value": 92}, reason="recount", author_id=DR[0], author_role=DR[1])
        with self.assertRaises(ApprovalError) as cm:
            self.approve(doc, 1)
        self.assertTrue(any("stale" in p for p in cm.exception.problems))

    def test_red_s_and_red_a_block_nil_o_p_do_not(self):
        # only O + P -> S and A are RED
        self.ev(EventCategory.VITAL, "pulse", {"value": 88})
        doc, _ = self.create()
        problems = check_approval(self.store, doc.id, 1, Role.CONSULTANT)
        self.assertIn("required section S has no supported information", problems)
        self.assertIn("required section A has no supported information", problems)
        # S + A only -> O and P are NIL, approval allowed
        enc = self.admit("D05")
        self.ev(EventCategory.SYMPTOM, "present", {"term": "fever"}, enc=enc)
        self.ev(EventCategory.DIAGNOSIS, "provisional", {"text": "viral fever"}, enc=enc)
        doc2, v = self.create(enc)
        self.assertEqual([l["color"] for s in v.content["sections"] if s["key"] in "OP" for l in s["lines"]], ["NIL", "NIL"])
        self.assertEqual(check_approval(self.store, doc2.id, 1, Role.CONSULTANT), [])
        self.approve(doc2, 1)

    def test_red_s_only_blocks(self):
        self.ev(EventCategory.DIAGNOSIS, "provisional", {"text": "viral fever"})
        doc, _ = self.create()
        self.assertEqual(check_approval(self.store, doc.id, 1, Role.CONSULTANT),
                         ["required section S has no supported information"])

    def test_red_a_only_blocks(self):
        self.ev(EventCategory.SYMPTOM, "present", {"term": "fever"})
        doc, _ = self.create()
        self.assertEqual(check_approval(self.store, doc.id, 1, Role.CONSULTANT),
                         ["required section A has no supported information"])

    def test_conflict_blocks_approval(self):
        self.full_timeline()
        self.tl.append(Event(encounter_id=self.enc, occurred_at=T0 + 2 * H, category=EventCategory.VITAL,
                             subtype="pulse", payload={"value": 120}, author_id="dev_monitor", author_role=Role.DEVICE,
                             source_type=SourceType.DEVICE, confidence=1.0, verification=Verification.AUTO))
        doc, _ = self.create()
        problems = check_approval(self.store, doc.id, 1, Role.CONSULTANT)
        self.assertIn("unresolved conflicts in the note", problems)
        self.assertTrue(any("requires review" in p for p in problems))
        with self.assertRaises(ApprovalError):
            self.approve(doc, 1)

    def test_non_current_version_cannot_be_approved(self):
        ev = self.full_timeline()
        doc, _ = self.create()
        self.ev(EventCategory.PLAN, "followup", {"text": "Review tomorrow"}, at=T0 + 4 * H)
        regenerate_progress_note(self.store, doc.id, generated_by=DR[0], generated_role=DR[1], generated_at=T0 + 5 * H)
        with self.assertRaises(ApprovalError) as cm:
            self.approve(doc, 1)
        self.assertTrue(any("not the current version" in p for p in cm.exception.problems))
        self.assertEqual(self.approve(doc, 2).version.version, 2)
        self.assertIsNone(self.store.get_document_version(doc.id, 1).approved_at)
        self.assertTrue(ev)

    def test_superseded_or_retracted_reference_blocks(self):
        ev = self.full_timeline()
        doc, _ = self.create()
        self.tl.retract(ev["dx"].id, reason="entered in error", author_id=DR[0], author_role=DR[1])
        problems = check_approval(self.store, doc.id, 1, Role.CONSULTANT)
        self.assertTrue(any("references retracted event" in p for p in problems))

    def test_discharged_encounter_not_reviewable(self):
        self.full_timeline()
        doc, _ = self.create()
        self.store.set_encounter_status(self.enc, EncounterStatus.DISCHARGED)
        self.assertTrue(any("reviewable" in p for p in check_approval(self.store, doc.id, 1, Role.CONSULTANT)))


class TestForgedVersions(DocBase):
    """Versions written around the API must still fail approval checks."""

    def setUp(self):
        super().setUp()
        self.ev_map = self.full_timeline()
        self.doc, self.v1 = self.create()

    def forge(self, mutate):
        c = copy.deepcopy(self.v1.content)
        mutate(c)
        ids = sorted({i for s in c["sections"] for l in s["lines"] for i in l["event_ids"]})
        self.store.add_document_version(DocumentVersion(document_id=self.doc.id, version=2, content=c,
                                                        source_event_ids=ids, generator="forged", created_by="x"))
        self.store.update_document(self.doc.id, 2, DocumentStatus.DRAFT)
        return check_approval(self.store, self.doc.id, 2, Role.CONSULTANT)

    def _line(self, c, key, i=0):
        return next(s for s in c["sections"] if s["key"] == key)["lines"][i]

    def test_every_clinical_line_needs_provenance(self):
        problems = self.forge(lambda c: self._line(c, "A").update(event_ids=[]))
        self.assertTrue(any("has no source event" in p for p in problems))

    def test_cross_encounter_reference_rejected(self):
        other = self.admit("D07")
        foreign = self.ev(EventCategory.DIAGNOSIS, "provisional", {"text": "sepsis"}, enc=other)
        problems = self.forge(lambda c: self._line(c, "A").update(event_ids=[foreign.id]))
        self.assertTrue(any("from another encounter" in p for p in problems))

    def test_missing_event_reference_rejected(self):
        problems = self.forge(lambda c: self._line(c, "A").update(event_ids=["evt_does_not_exist"]))
        self.assertTrue(any("missing event" in p for p in problems))

    def test_placeholder_with_fake_ids_rejected(self):
        def m(c):
            sec = next(s for s in c["sections"] if s["key"] == "S")
            sec["lines"] = [{"text": "No subjective information captured.", "event_ids": [self.ev_map["fever"].id],
                             "kind": "placeholder", "color": "RED", "needs_review": False, "review_reason": ""}]
        self.assertTrue(any("placeholder carries event ids" in p for p in self.forge(m)))

    def test_medication_must_read_as_proposed(self):
        problems = self.forge(lambda c: self._line(c, "P").update(text="Paracetamol 650 mg prescribed."))
        self.assertTrue(any("must read as proposed" in p for p in problems))


class TestSafetyAndRegression(unittest.TestCase):
    def test_no_llm_imports(self):
        import ast
        with open(docs_mod.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        mods = {(("." * n.level) + (n.module or "")) if isinstance(n, ast.ImportFrom) else a.name
                for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in (n.names if isinstance(n, ast.Import) else [n])}
        self.assertEqual(mods, {"copy", "re", "dataclasses", "datetime", "typing", ".models", ".progress_note",
                                ".store", ".timeline"})

    def test_er_extraction_unchanged(self):
        import stt_extract
        h = lambda o: hashlib.sha256(json.dumps(o, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]  # noqa: E731
        got = {}
        for job in ("demo-001", "demo-002"):
            tj, ej = stt_extract.run_stt_extract(os.path.join(_DEMO, "cleaned", f"{job}.wav"), job, use_llm=False,
                                                 model="mock")
            got[job] = (h(tj), h(ej))
        self.assertEqual(got, {"demo-001": ("43c32d9c00ada83f", "5dcdbfa65b309640"),
                               "demo-002": ("496c0d18c80a7ad2", "89d94317c2ae36a8")})


import unittest.mock  # noqa: E402  (used by test_create_is_transactional)

if __name__ == "__main__":
    unittest.main()
