"""IPD P1 step 2: transactional admission (in-memory SQLite, synthetic data).

Run:  python -m unittest demo/tests/test_ipd_encounter.py -v   (from repo root)
Covers: patient / encounter / bed / ADT event / audit created together,
deterministic event payload, timezone-aware timestamps, rollback when a
later step fails, input validation, no writes to existing ER directories.
"""
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO = os.path.dirname(_HERE)
_REPO = os.path.dirname(_DEMO)
sys.path.insert(0, _DEMO)

from ipd.encounter import ADMITTING_ROLES, admit_patient, synthetic_patient  # noqa: E402
from ipd.models import (  # noqa: E402
    IST, AuditAction, EncounterStatus, EncounterType, EventCategory, Patient, Role, SourceType,
    UnitType, Verification,
)
from ipd.store import IpdStore  # noqa: E402

T0 = datetime(2026, 10, 6, 9, 15, tzinfo=IST)
ER_DIRS = ("entities", "exports", "transcripts", "cleaned", "_state", "templates", "audio_in", "assets")


def _admit(store, patient=None, **kw):
    args = dict(ward="Ward 4", bed="12", admitted_by="u_dr_demo", admitted_role=Role.CONSULTANT, admit_at=T0)
    args.update(kw)
    return admit_patient(store, patient or synthetic_patient("001"), **args)


def _nothing_persisted(test, store, patient_id):
    test.assertIsNone(store.get_patient(patient_id))
    test.assertEqual(store.list_encounters(patient_id), [])
    for table in ("encounters", "bed_assignments", "events", "audit_log"):
        test.assertEqual(store._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)


class TestAdmission(unittest.TestCase):
    def setUp(self):
        self.store = IpdStore()

    def tearDown(self):
        self.store.close()

    def test_successful_admission_creates_all_records(self):
        r = _admit(self.store)
        s = self.store
        # patient
        self.assertEqual(s.get_patient(r.patient.id), r.patient)
        self.assertTrue(r.patient.mrn.startswith("DEMO-"))
        # encounter
        enc = s.get_encounter(r.encounter.id)
        self.assertEqual(enc, r.encounter)
        self.assertEqual(enc.patient_id, r.patient.id)
        self.assertEqual(enc.type, EncounterType.IPD)
        self.assertEqual(enc.status, EncounterStatus.IN_WARD)
        self.assertEqual(enc.admit_at, T0)
        self.assertIsNone(enc.discharge_at)
        # bed
        self.assertEqual(s.get_active_bed(enc.id), r.bed)
        self.assertEqual((r.bed.ward, r.bed.bed, r.bed.unit_type, r.bed.from_at, r.bed.to_at),
                         ("Ward 4", "12", UnitType.WARD, T0, None))
        # ADT event
        events = s.list_events(enc.id)
        self.assertEqual(events, [r.event])
        ev = events[0]
        self.assertEqual(ev.encounter_id, enc.id)
        self.assertEqual((ev.category, ev.subtype), (EventCategory.ADT, "admitted"))
        self.assertEqual((ev.author_id, ev.author_role), ("u_dr_demo", Role.CONSULTANT))
        self.assertEqual((ev.source_type, ev.verification, ev.confidence),
                         (SourceType.TYPED, Verification.VERIFIED, 1.0))
        self.assertEqual(ev.occurred_at, T0)
        # audit
        audit = s.list_audit("encounter", enc.id)
        self.assertEqual(audit, [r.audit])
        a = audit[0]
        self.assertEqual((a.action, a.user_id, a.role), (AuditAction.CREATE, "u_dr_demo", Role.CONSULTANT))
        self.assertEqual(a.detail["event_id"], ev.id)
        self.assertEqual(a.detail["bed_assignment_id"], r.bed.id)
        self.assertEqual(a.detail["patient_id"], r.patient.id)

    def test_event_payload_is_only_admission_data(self):
        r = _admit(self.store)
        self.assertEqual(r.event.payload, {"ward": "Ward 4", "bed": "12", "unit_type": "WARD",
                                           "encounter_status": "IN_WARD", "bed_assignment_id": r.bed.id})
        self.assertEqual(r.event.codes, [])
        self.assertIsNone(r.event.source_capture_id)
        self.assertEqual(r.event.source_span, {})
        self.assertIsNone(r.event.supersedes_event_id)

    def test_event_belongs_to_its_own_encounter(self):
        r1 = _admit(self.store, synthetic_patient("001"))
        r2 = _admit(self.store, synthetic_patient("002"), ward="Ward 5", bed="3")
        self.assertEqual(self.store.list_events(r1.encounter.id), [r1.event])
        self.assertEqual(self.store.list_events(r2.encounter.id), [r2.event])
        self.assertEqual(r2.event.payload["ward"], "Ward 5")

    def test_icu_admission_status_and_linked_encounter(self):
        opd = _admit(self.store, synthetic_patient("003"))
        r = _admit(self.store, opd.patient, ward="ICU-3", bed="2", unit_type=UnitType.ICU,
                   admitted_role=Role.INTENSIVIST, linked_encounter_id=opd.encounter.id)
        self.assertEqual(r.encounter.status, EncounterStatus.IN_ICU)
        self.assertEqual(r.encounter.linked_encounter_id, opd.encounter.id)
        self.assertEqual(r.event.payload["unit_type"], "ICU")
        self.assertEqual(r.event.payload["linked_encounter_id"], opd.encounter.id)
        # existing patient reused, not duplicated
        self.assertEqual(len(self.store.list_encounters(opd.patient.id)), 2)
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0], 1)

    def test_timestamps_are_timezone_aware(self):
        r = _admit(self.store)
        enc = self.store.get_encounter(r.encounter.id)
        bed = self.store.get_active_bed(r.encounter.id)
        ev = self.store.list_events(r.encounter.id)[0]
        au = self.store.list_audit("encounter", r.encounter.id)[0]
        for ts in (enc.admit_at, bed.from_at, ev.occurred_at, ev.recorded_at, au.at):
            self.assertIsNotNone(ts.tzinfo)
            self.assertIsNotNone(ts.utcoffset())

    def test_default_admit_time_is_now_and_aware(self):
        before = datetime.now(timezone.utc)
        r = admit_patient(self.store, synthetic_patient("004"), ward="Ward 4", bed="1",
                          admitted_by="u_admin", admitted_role=Role.ADMIN)
        after = datetime.now(timezone.utc)
        self.assertTrue(before <= r.encounter.admit_at <= after)
        self.assertEqual(r.encounter.admit_at, r.event.occurred_at)
        self.assertEqual(r.encounter.admit_at, r.bed.from_at)


class TestAdmissionRollback(unittest.TestCase):
    def setUp(self):
        self.store = IpdStore()

    def tearDown(self):
        self.store.close()

    def _assert_rolls_back_when(self, method, exc=RuntimeError("injected failure")):
        p = synthetic_patient("009")
        with mock.patch.object(self.store, method, side_effect=exc):
            with self.assertRaises(type(exc)):
                _admit(self.store, p)
        _nothing_persisted(self, self.store, p.id)

    def test_rollback_when_audit_fails(self):
        self._assert_rolls_back_when("append_audit")

    def test_rollback_when_event_fails(self):
        self._assert_rolls_back_when("append_event")

    def test_rollback_when_bed_fails(self):
        self._assert_rolls_back_when("add_bed_assignment")

    def test_rollback_on_database_constraint(self):
        existing = _admit(self.store, synthetic_patient("010"))
        clash = Patient(mrn=existing.patient.mrn, name="Other synthetic")  # same MRN, new id
        with self.assertRaises(sqlite3.IntegrityError):
            _admit(self.store, clash)
        self.assertIsNone(self.store.get_patient(clash.id))
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM encounters").fetchone()[0], 1)
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_store_usable_after_rollback(self):
        self._assert_rolls_back_when("append_audit")
        r = _admit(self.store, synthetic_patient("011"))
        self.assertEqual(self.store.list_events(r.encounter.id), [r.event])


class TestAdmissionValidation(unittest.TestCase):
    def setUp(self):
        self.store = IpdStore()

    def tearDown(self):
        self.store.close()

    def test_invalid_inputs_write_nothing(self):
        p = synthetic_patient("020")
        cases = [dict(ward=""), dict(bed="  "), dict(admitted_by=""),
                 dict(admitted_role=Role.WARD_NURSE), dict(admitted_role=Role.DEVICE),
                 dict(admit_at=datetime(2026, 10, 6, 9, 15)),  # naive
                 dict(unit_type="THEATRE")]
        for kw in cases:
            with self.subTest(**{k: str(v) for k, v in kw.items()}):
                with self.assertRaises(ValueError):
                    _admit(self.store, p, **kw)
                _nothing_persisted(self, self.store, p.id)

    def test_admitting_roles(self):
        self.assertIn(Role.CONSULTANT, ADMITTING_ROLES)
        self.assertNotIn(Role.DEVICE, ADMITTING_ROLES)


class TestNoWritesOutsideStore(unittest.TestCase):
    def _snapshot(self):
        snap = {}
        for d in ER_DIRS:
            root = os.path.join(_DEMO, d)
            for base, _, files in os.walk(root):
                for f in files:
                    fp = os.path.join(base, f)
                    st = os.stat(fp)
                    snap[os.path.relpath(fp, _REPO)] = (st.st_size, st.st_mtime_ns)
        return snap

    def _db_files(self):
        found = []
        for base, dirs, files in os.walk(_REPO):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules")]
            found += [os.path.join(base, f) for f in files if f.endswith((".db", ".sqlite", ".sqlite3"))]
        return found

    def test_admission_touches_no_er_directories_or_db_files(self):
        before, dbs_before = self._snapshot(), self._db_files()
        with IpdStore() as store:
            _admit(store, synthetic_patient("030"))
            _admit(store, synthetic_patient("031"), ward="ICU-1", bed="4", unit_type=UnitType.ICU,
                   admitted_role=Role.INTENSIVIST, admit_at=T0 + timedelta(hours=1))
            self.assertEqual(store.path, ":memory:")
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._db_files(), dbs_before)


if __name__ == "__main__":
    unittest.main()
