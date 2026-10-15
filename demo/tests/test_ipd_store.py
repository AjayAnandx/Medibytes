"""IPD P1 step 1: models + SQLite store (in-memory only).

Run:  python -m unittest demo.tests.test_ipd_store -v   (from repo root)
  or: python -m pytest demo/tests/test_ipd_store.py -q
Covers: schema, round-trips, timezone-aware timestamps, strict JSON,
append-only events/audit/source records, supersede rules, immutable
document-version content, approval-once, transactions.
"""
import os
import sqlite3
import sys
import unittest
from datetime import date, datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO = os.path.dirname(_HERE)
sys.path.insert(0, _DEMO)

from ipd.models import (  # noqa: E402
    IST, AuditAction, AuditEntry, BedAssignment, Capture, CaptureContext, CaptureSource, CaptureStatus,
    ChangeType, Document, DocumentStatus, DocumentType, DocumentVersion, Encounter, EncounterStatus,
    Event, EventCategory, Patient, Role, SourceRecord, SourceType, UnitType, Verification,
)
from ipd.store import SCHEMA_VERSION, IpdStore  # noqa: E402

T0 = datetime(2026, 10, 6, 8, 30, tzinfo=IST)


def _event(enc_id, **kw):
    base = dict(encounter_id=enc_id, occurred_at=T0, category=EventCategory.VITAL, subtype="spo2",
                author_id="u_dr_rao", author_role=Role.CONSULTANT, source_type=SourceType.SPOKEN,
                payload={"value": 92, "unit": "%"}, confidence=0.9)
    base.update(kw)
    return Event(**base)


class StoreTestBase(unittest.TestCase):
    def setUp(self):
        self.store = IpdStore()  # in-memory
        self.patient = self.store.add_patient(Patient(mrn="MRN-0001", name="Test Patient",
                                                      dob=date(1981, 5, 2), sex="M"))
        self.enc = self.store.add_encounter(Encounter(patient_id=self.patient.id, admit_at=T0))

    def tearDown(self):
        self.store.close()


class TestSchemaAndSetup(StoreTestBase):
    def test_in_memory_by_default(self):
        self.assertEqual(self.store.path, ":memory:")
        self.assertEqual(IpdStore().path, ":memory:")

    def test_schema_version_and_foreign_keys(self):
        conn = self.store._conn
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("patients", "encounters", "bed_assignments", "captures", "source_records",
                  "events", "documents", "document_versions", "audit_log"):
            self.assertIn(t, tables)

    def test_stores_are_isolated(self):
        other = IpdStore()
        self.assertIsNone(other.get_patient(self.patient.id))
        other.close()

    def test_no_update_or_delete_api_for_events(self):
        for name in dir(self.store):
            if "event" in name and any(w in name for w in ("update", "delete", "remove", "edit", "set_")):
                self.fail(f"unexpected mutating event API: {name}")


class TestPatientEncounterBed(StoreTestBase):
    def test_patient_roundtrip_and_mrn_unique(self):
        self.assertEqual(self.store.get_patient(self.patient.id), self.patient)
        self.assertEqual(self.store.get_patient_by_mrn("MRN-0001"), self.patient)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_patient(Patient(mrn="MRN-0001", name="Duplicate"))

    def test_encounter_roundtrip_and_status(self):
        got = self.store.get_encounter(self.enc.id)
        self.assertEqual(got, self.enc)
        self.assertEqual(got.type.value, "IPD")
        self.store.set_encounter_status(self.enc.id, EncounterStatus.IN_WARD)
        self.assertEqual(self.store.get_encounter(self.enc.id).status, EncounterStatus.IN_WARD)
        with self.assertRaises(KeyError):
            self.store.set_encounter_status("enc_missing", EncounterStatus.IN_WARD)
        self.assertEqual([e.id for e in self.store.list_encounters(self.patient.id)], [self.enc.id])

    def test_encounter_requires_existing_patient(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_encounter(Encounter(patient_id="pat_missing", admit_at=T0))

    def test_bed_assignment_active_and_close(self):
        b1 = self.store.add_bed_assignment(BedAssignment(encounter_id=self.enc.id, ward="Ward 4",
                                                         bed="12", from_at=T0))
        self.assertEqual(self.store.get_active_bed(self.enc.id), b1)
        self.store.close_bed_assignment(b1.id, T0 + timedelta(days=1))
        b2 = self.store.add_bed_assignment(BedAssignment(encounter_id=self.enc.id, ward="ICU-3", bed="2",
                                                         unit_type=UnitType.ICU, from_at=T0 + timedelta(days=1)))
        self.assertEqual(self.store.get_active_bed(self.enc.id).id, b2.id)
        self.assertEqual([b.ward for b in self.store.list_bed_assignments(self.enc.id)], ["Ward 4", "ICU-3"])
        with self.assertRaises(KeyError):
            self.store.close_bed_assignment(b1.id, T0 + timedelta(days=2))  # already closed


class TestCaptureAndSource(StoreTestBase):
    def _capture(self):
        return self.store.add_capture(Capture(
            encounter_id=self.enc.id, source=CaptureSource.TEXT, capture_context=CaptureContext.WARD_ROUND,
            author_id="u_dr_rao", author_role=Role.CONSULTANT, captured_at=T0,
            location={"ward": "Ward 4", "bed": "12", "unit_type": "WARD"}))

    def test_capture_roundtrip_and_status(self):
        c = self._capture()
        self.assertEqual(self.store.get_capture(c.id), c)
        self.store.set_capture_status(c.id, CaptureStatus.PROCESSED, {"engine": "typed", "llm_engine": "none"})
        got = self.store.get_capture(c.id)
        self.assertEqual(got.status, CaptureStatus.PROCESSED)
        self.assertEqual(got.pipeline_info["engine"], "typed")

    def test_source_record_roundtrip_and_immutable(self):
        c = self._capture()
        s = self.store.add_source_record(SourceRecord(
            capture_id=c.id, text="SpO₂ 92% on room air. बुखार नहीं.", normalized_text="SpO2 92% on room air.",
            segments=[{"id": 0, "text": "SpO₂ 92% on room air.", "start": 0.0, "end": 2.1}], engine="typed"))
        self.assertEqual(self.store.get_source_record(c.id), s)
        with self.assertRaises(sqlite3.DatabaseError):
            self.store._conn.execute("UPDATE source_records SET text = 'x' WHERE capture_id = ?", (c.id,))
        with self.assertRaises(sqlite3.DatabaseError):
            self.store._conn.execute("DELETE FROM source_records WHERE capture_id = ?", (c.id,))

    def test_capture_requires_existing_encounter(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_capture(Capture(encounter_id="enc_missing", source="text", capture_context="ward_round",
                                           author_id="u1", author_role="RESIDENT", captured_at=T0))


class TestEvents(StoreTestBase):
    def test_event_roundtrip_all_fields(self):
        e = _event(self.enc.id, codes=[{"system": "LOINC", "code": "59408-5", "display": "SpO₂"}],
                   source_span={"segment_id": 0, "start_ms": 0, "end_ms": 2100, "char_start": 0, "char_end": 8},
                   verification=Verification.UNVERIFIED)
        self.store.append_event(e)
        got = self.store.get_event(e.id)
        self.assertEqual(got, e)
        self.assertIsNotNone(got.occurred_at.tzinfo)
        self.assertEqual(got.payload, {"value": 92, "unit": "%"})
        self.assertEqual(got.codes[0]["display"], "SpO₂")

    def test_events_are_append_only(self):
        e = self.store.append_event(_event(self.enc.id))
        conn = self.store._conn
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("UPDATE events SET payload = '{}' WHERE id = ?", (e.id,))
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM events WHERE id = ?", (e.id,))
        self.assertEqual(self.store.get_event(e.id), e)

    def test_duplicate_event_id_rejected(self):
        e = self.store.append_event(_event(self.enc.id))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.append_event(e)

    def test_event_is_frozen(self):
        e = _event(self.enc.id)
        with self.assertRaises(Exception):
            e.subtype = "hr"

    def test_supersede_and_retraction(self):
        old = self.store.append_event(_event(self.enc.id))
        new = self.store.append_event(_event(self.enc.id, payload={"value": 94, "unit": "%"},
                                             supersedes_event_id=old.id, reason="misheard value",
                                             verification=Verification.VERIFIED))
        self.assertEqual(self.store.superseded_event_ids(self.enc.id), {old.id})
        self.assertEqual(self.store.get_event(old.id), old)  # original still there, unchanged
        retr = self.store.append_event(_event(self.enc.id, supersedes_event_id=new.id,
                                              reason="captured for wrong patient", retraction=True))
        self.assertTrue(self.store.get_event(retr.id).retraction)
        self.assertEqual(self.store.superseded_event_ids(self.enc.id), {old.id, new.id})

    def test_supersede_rules(self):
        with self.assertRaises(ValueError):
            _event(self.enc.id, supersedes_event_id="evt_x")  # no reason
        with self.assertRaises(ValueError):
            _event(self.enc.id, retraction=True, reason="r")  # retraction without target
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.append_event(_event(self.enc.id, supersedes_event_id="evt_missing", reason="r"))
        other_enc = self.store.add_encounter(Encounter(patient_id=self.patient.id, admit_at=T0))
        foreign = self.store.append_event(_event(other_enc.id))
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.append_event(_event(self.enc.id, supersedes_event_id=foreign.id, reason="r"))

    def test_list_events_filters_and_order(self):
        e_late = self.store.append_event(_event(self.enc.id, occurred_at=T0 + timedelta(hours=2)))
        e_early = self.store.append_event(_event(self.enc.id, occurred_at=T0))
        # same instant expressed in UTC must sort with IST values correctly
        e_mid_utc = self.store.append_event(_event(self.enc.id, category=EventCategory.SYMPTOM, subtype="fever",
                                                   occurred_at=(T0 + timedelta(hours=1)).astimezone(timezone.utc)))
        ids = [e.id for e in self.store.list_events(self.enc.id)]
        self.assertEqual(ids, [e_early.id, e_mid_utc.id, e_late.id])
        self.assertEqual([e.id for e in self.store.list_events(self.enc.id, since=T0 + timedelta(hours=1))],
                         [e_mid_utc.id, e_late.id])
        self.assertEqual([e.id for e in self.store.list_events(self.enc.id, until=T0 + timedelta(hours=1))],
                         [e_early.id])
        self.assertEqual([e.id for e in self.store.list_events(self.enc.id, categories=["SYMPTOM"])],
                         [e_mid_utc.id])
        self.assertEqual(self.store.list_events(self.enc.id, categories=[]), [])

    def test_event_validation(self):
        with self.assertRaises(ValueError):
            _event(self.enc.id, occurred_at=datetime(2026, 10, 6, 8, 30))  # naive
        with self.assertRaises(ValueError):
            _event(self.enc.id, confidence=1.5)
        with self.assertRaises(ValueError):
            _event(self.enc.id, category="NOT_A_CATEGORY")
        with self.assertRaises(ValueError):
            _event(self.enc.id, subtype="")
        self.assertEqual(_event(self.enc.id, category="SYMPTOM").category, EventCategory.SYMPTOM)

    def test_strict_json(self):
        with self.assertRaises(ValueError):
            self.store.append_event(_event(self.enc.id, payload={"value": float("nan")}))
        with self.assertRaises(ValueError):
            self.store.append_event(_event(self.enc.id, payload={"when": T0}))  # datetime is not JSON
        self.assertEqual(self.store.list_events(self.enc.id), [])


class TestTimestamps(StoreTestBase):
    def test_stored_utc_returned_aware_and_equal(self):
        e = self.store.append_event(_event(self.enc.id))
        raw = self.store._conn.execute("SELECT occurred_at FROM events WHERE id = ?", (e.id,)).fetchone()[0]
        self.assertEqual(raw, "2026-10-06T03:00:00.000000+00:00")
        got = self.store.get_event(e.id).occurred_at
        self.assertEqual(got, T0)
        self.assertEqual(got.astimezone(IST).isoformat(), "2026-10-06T08:30:00+05:30")

    def test_naive_rejected_everywhere(self):
        naive = datetime(2026, 10, 6, 8, 30)
        with self.assertRaises(ValueError):
            Encounter(patient_id=self.patient.id, admit_at=naive)
        with self.assertRaises(ValueError):
            self.store.list_events(self.enc.id, since=naive)
        with self.assertRaises(ValueError):
            self.store.close_bed_assignment("bed_x", naive)


class TestDocuments(StoreTestBase):
    def _doc_v1(self):
        d = self.store.add_document(Document(encounter_id=self.enc.id, type=DocumentType.PROGRESS_NOTE,
                                             window_from=T0))
        v = self.store.add_document_version(DocumentVersion(
            document_id=d.id, version=1, content={"S": [{"text": "fever", "event_ids": ["evt_1"], "color": "YELLOW"}]},
            source_event_ids=["evt_1"], generator="progress_note/p1", created_by="system"))
        self.store.update_document(d.id, 1, DocumentStatus.DRAFT)
        return d, v

    def test_document_and_version_roundtrip(self):
        d, v = self._doc_v1()
        self.assertEqual(self.store.get_document(d.id).current_version, 1)
        self.assertEqual(self.store.get_document_version(d.id, 1), v)
        v2 = self.store.add_document_version(DocumentVersion(
            document_id=d.id, version=2, content={"S": []}, source_event_ids=[], generator="progress_note/p1",
            created_by="u_dr_rao", change_type=ChangeType.WORDING_EDIT))
        self.assertEqual([x.version for x in self.store.list_document_versions(d.id)], [1, 2])
        self.assertEqual(self.store.list_document_versions(d.id)[1], v2)
        self.assertEqual([x.id for x in self.store.list_documents(self.enc.id, DocumentType.PROGRESS_NOTE)], [d.id])

    def test_version_content_immutable_and_unique(self):
        d, v = self._doc_v1()
        with self.assertRaises(sqlite3.DatabaseError):
            self.store._conn.execute("UPDATE document_versions SET content = '{}' WHERE document_id = ?", (d.id,))
        with self.assertRaises(sqlite3.DatabaseError):
            self.store._conn.execute("DELETE FROM document_versions WHERE document_id = ?", (d.id,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_document_version(v)
        with self.assertRaises(ValueError):
            DocumentVersion(document_id=d.id, version=0, content={}, source_event_ids=[], generator="g",
                            created_by="u")

    def test_stale_and_approval_once(self):
        d, _ = self._doc_v1()
        self.store.mark_version_stale(d.id, 1)
        self.assertTrue(self.store.get_document_version(d.id, 1).stale)
        at = T0 + timedelta(hours=3)
        self.store.record_version_approval(d.id, 1, "u_dr_rao", Role.CONSULTANT, at)
        got = self.store.get_document_version(d.id, 1)
        self.assertEqual((got.approved_by, got.approved_role, got.approved_at), ("u_dr_rao", Role.CONSULTANT, at))
        with self.assertRaises(KeyError):
            self.store.record_version_approval(d.id, 1, "u_other", Role.RESIDENT, at)


class TestAuditAndTransactions(StoreTestBase):
    def test_audit_append_only(self):
        a = self.store.append_audit(AuditEntry(user_id="u_dr_rao", role=Role.CONSULTANT, action=AuditAction.CREATE,
                                               entity="encounter", entity_id=self.enc.id, detail={"note": "admit"}))
        self.assertEqual(self.store.list_audit("encounter", self.enc.id), [a])
        with self.assertRaises(sqlite3.DatabaseError):
            self.store._conn.execute("UPDATE audit_log SET action = 'view'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store._conn.execute("DELETE FROM audit_log")

    def test_transaction_rolls_back_as_a_unit(self):
        e1 = _event(self.enc.id)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction():
                self.store.append_event(e1)
                self.store.append_event(e1)  # duplicate id -> whole group rolls back
        self.assertEqual(self.store.list_events(self.enc.id), [])
        with self.store.transaction():
            self.store.append_event(e1)
            self.store.append_audit(AuditEntry(user_id="u1", role="RESIDENT", action="create",
                                               entity="event", entity_id=e1.id))
        self.assertEqual(len(self.store.list_events(self.enc.id)), 1)
        self.assertEqual(len(self.store.list_audit("event", e1.id)), 1)


if __name__ == "__main__":
    unittest.main()
