"""IPD P1 step 4: Patient Event Timeline (in-memory SQLite, synthetic data).

Run:  python -m unittest demo/tests/test_ipd_timeline.py -v   (from repo root)
Covers: append / batch append (atomic), queries and filters, encounter
isolation, correction and retraction as new events, active view, duplicate
handling, validation, audit, conflict detection without resolution.
"""
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO = os.path.dirname(_HERE)
sys.path.insert(0, _DEMO)

from ipd.encounter import admit_patient, synthetic_patient  # noqa: E402
from ipd.event_extractor import extract_events  # noqa: E402
from ipd.models import (  # noqa: E402
    IST, AuditAction, Capture, CaptureContext, CaptureSource, EncounterStatus, Event, EventCategory, Role,
    SourceType, Verification,
)
from ipd.store import IpdStore  # noqa: E402
from ipd.timeline import Timeline, TimelineError  # noqa: E402

T0 = datetime(2026, 10, 6, 8, 0, tzinfo=IST)
H = timedelta(hours=1)


class TimelineBase(unittest.TestCase):
    def setUp(self):
        self.store = IpdStore()
        self.tl = Timeline(self.store)
        self.adm = admit_patient(self.store, synthetic_patient("T01"), ward="Ward 4", bed="12",
                                 admitted_by="u_dr_demo", admitted_role=Role.CONSULTANT, admit_at=T0)
        self.enc = self.adm.encounter.id
        self.adm2 = admit_patient(self.store, synthetic_patient("T02"), ward="Ward 5", bed="3",
                                  admitted_by="u_dr_demo", admitted_role=Role.CONSULTANT, admit_at=T0)
        self.enc2 = self.adm2.encounter.id

    def tearDown(self):
        self.store.close()

    def ev(self, enc=None, *, at=None, category=EventCategory.VITAL, subtype="temperature", payload=None,
           verification=Verification.UNVERIFIED, **kw):
        return Event(encounter_id=enc or self.enc, occurred_at=at or T0 + H, category=category, subtype=subtype,
                     payload={"value": 101, "unit": "F"} if payload is None else payload,
                     author_id="u_nurse_demo", author_role=Role.WARD_NURSE, source_type=SourceType.SPOKEN,
                     confidence=0.87, verification=verification, **kw)

    def clinical(self, enc=None, **kw):
        """history without the admission ADT event"""
        return [e for e in self.tl.history(enc or self.enc, **kw) if e.category != EventCategory.ADT]

    def audits(self, event_id):
        return self.store.list_audit("event", event_id)


class TestAppendAndQuery(TimelineBase):
    def test_append_one(self):
        e = self.ev()
        r = self.tl.append(e)
        self.assertEqual((r.appended, r.duplicates), ((e,), ()))
        self.assertEqual(self.store.get_event(e.id), e)
        self.assertEqual(self.clinical(), [e])

    def test_append_many(self):
        batch = [self.ev(subtype="temperature"), self.ev(subtype="spo2", payload={"value": 95, "unit": "%"}),
                 self.ev(subtype="pulse", payload={"value": 88})]
        r = self.tl.append_many(self.enc, batch)
        self.assertEqual(list(r.appended), batch)
        self.assertEqual(self.clinical(), batch)

    def test_chronological_order(self):
        late, early, mid = self.ev(at=T0 + 3 * H), self.ev(at=T0 + H), self.ev(at=T0 + 2 * H)
        for e in (late, early, mid):
            self.tl.append(e)
        self.assertEqual(self.clinical(), [early, mid, late])
        self.assertEqual(self.tl.history(self.enc)[0].category, EventCategory.ADT)  # admission first (T0)

    def test_since_until_category_filters(self):
        a = self.ev(at=T0 + H)
        b = self.ev(at=T0 + 2 * H, category=EventCategory.SYMPTOM, subtype="present",
                    payload={"term": "fever", "negated": False})
        c = self.ev(at=T0 + 3 * H)
        self.tl.append_many(self.enc, [a, b, c])
        self.assertEqual(self.clinical(since=T0 + 2 * H), [b, c])
        self.assertEqual(self.clinical(until=T0 + 2 * H), [a])
        self.assertEqual(self.tl.history(self.enc, categories=[EventCategory.SYMPTOM]), [b])
        self.assertEqual(self.tl.history(self.enc, categories=["VITAL"]), [a, c])

    def test_verification_filter(self):
        u = self.ev(verification=Verification.UNVERIFIED)
        v = self.ev(at=T0 + 2 * H, verification=Verification.VERIFIED)
        self.tl.append_many(self.enc, [u, v])
        self.assertEqual(self.clinical(verification=[Verification.UNVERIFIED]), [u])
        self.assertEqual(self.tl.history(self.enc, verification=["verified"])[-1], v)

    def test_encounter_isolation(self):
        mine, theirs = self.ev(), self.ev(self.enc2, payload={"value": 99, "unit": "F"})
        self.tl.append(mine)
        self.tl.append(theirs)
        self.assertEqual(self.clinical(), [mine])
        self.assertEqual(self.clinical(self.enc2), [theirs])
        with self.assertRaises(TimelineError):
            self.tl.append_many(self.enc, [theirs.__class__(**{**theirs.__dict__, "id": "evt_x"})])

    def test_unknown_encounter_query_rejected(self):
        with self.assertRaises(TimelineError):
            self.tl.history("enc_missing")


class TestCorrectionAndRetraction(TimelineBase):
    def setUp(self):
        super().setUp()
        self.e1 = self.ev(payload={"value": 101, "unit": "F", "text": "temperature 101 F"})
        self.tl.append(self.e1)

    def test_supersede_success_original_unchanged_and_active(self):
        before = self.store.get_event(self.e1.id)
        e2 = self.tl.correct(self.e1.id, payload={"value": 100.1, "unit": "F"}, reason="misheard reading",
                             author_id="u_dr_demo", author_role=Role.CONSULTANT, recorded_at=T0 + 2 * H)
        self.assertEqual(self.store.get_event(self.e1.id), before)  # untouched
        self.assertEqual((e2.supersedes_event_id, e2.reason, e2.encounter_id), (self.e1.id, "misheard reading", self.enc))
        self.assertEqual((e2.verification, e2.source_type, e2.author_id), (Verification.VERIFIED, SourceType.TYPED, "u_dr_demo"))
        self.assertEqual(e2.occurred_at, self.e1.occurred_at)
        self.assertEqual(e2.recorded_at, T0 + 2 * H)
        self.assertNotEqual(e2.recorded_at, self.e1.recorded_at)
        self.assertEqual(self.clinical(), [self.e1, e2])  # full history
        self.assertEqual([e for e in self.tl.active_events(self.enc) if e.category == EventCategory.VITAL], [e2])
        self.assertEqual((self.tl.status_of(self.e1.id), self.tl.status_of(e2.id)), ("superseded", "active"))
        self.assertEqual(self.tl.correction_chain(e2.id), [self.e1, e2])

    def test_correction_provenance_is_its_own(self):
        e1 = self.ev(source_span={"char_start": 0, "char_end": 10})
        self.tl.append(e1)
        e2 = self.tl.correct(e1.id, payload={"value": 99, "unit": "F"}, reason="typo",
                             author_id="u_dr_demo", author_role=Role.CONSULTANT)
        self.assertEqual(e2.source_span, {})
        self.assertIsNone(e2.source_capture_id)
        self.assertEqual(self.store.get_event(e1.id).source_span, {"char_start": 0, "char_end": 10})

    def test_supersede_requires_reason(self):
        for bad in ("", "   "):
            with self.assertRaises(TimelineError):
                self.tl.correct(self.e1.id, payload={"value": 100}, reason=bad,
                                author_id="u_dr_demo", author_role=Role.CONSULTANT)
        self.assertEqual(self.tl.status_of(self.e1.id), "active")

    def test_cross_encounter_supersede_rejected(self):
        other = self.ev(self.enc2)
        self.tl.append(other)
        bad = self.ev(supersedes_event_id=other.id, reason="wrong patient")  # in self.enc
        with self.assertRaises((TimelineError, sqlite3.DatabaseError)):
            self.tl.append(bad)
        self.assertIsNone(self.store.get_event(bad.id))
        self.assertEqual(self.tl.status_of(other.id), "active")

    def test_cannot_supersede_twice_or_change_category(self):
        self.tl.correct(self.e1.id, payload={"value": 100}, reason="r1", author_id="u", author_role=Role.RESIDENT)
        with self.assertRaises(TimelineError):
            self.tl.correct(self.e1.id, payload={"value": 99}, reason="r2", author_id="u", author_role=Role.RESIDENT)
        other = self.ev(at=T0 + 3 * H)
        self.tl.append(other)
        with self.assertRaises(TimelineError):
            self.tl.append(self.ev(category=EventCategory.SYMPTOM, subtype="present",
                                   payload={"term": "fever"}, supersedes_event_id=other.id, reason="x"))

    def test_retraction(self):
        r = self.tl.retract(self.e1.id, reason="recorded for wrong patient", author_id="u_dr_demo",
                            author_role=Role.CONSULTANT, recorded_at=T0 + 2 * H)
        self.assertTrue(r.retraction)
        self.assertEqual((r.supersedes_event_id, r.reason, r.author_id, r.author_role),
                         (self.e1.id, "recorded for wrong patient", "u_dr_demo", Role.CONSULTANT))
        self.assertIsNotNone(r.recorded_at.tzinfo)
        self.assertIn(self.e1, self.clinical())  # still in full history
        self.assertEqual(self.store.get_event(self.e1.id), self.e1)
        active = self.tl.active_events(self.enc)
        self.assertNotIn(self.e1.id, [e.id for e in active])
        self.assertNotIn(r.id, [e.id for e in active])  # the marker is not a fact
        self.assertEqual((self.tl.status_of(self.e1.id), self.tl.status_of(r.id)), ("retracted", "retraction"))
        with self.assertRaises(TimelineError):
            self.tl.retract(r.id, reason="undo", author_id="u", author_role=Role.RESIDENT)

    def test_retraction_requires_reason_and_target(self):
        with self.assertRaises(TimelineError):
            self.tl.retract(self.e1.id, reason="", author_id="u", author_role=Role.RESIDENT)
        with self.assertRaises(TimelineError):
            self.tl.retract("evt_missing", reason="r", author_id="u", author_role=Role.RESIDENT)
        with self.assertRaises(TimelineError):
            self.tl.retract(self.e1.id, reason="r", author_id="", author_role=Role.RESIDENT)
        self.assertEqual(self.tl.status_of(self.e1.id), "active")


class TestAtomicityDuplicatesValidation(TimelineBase):
    def test_batch_rollback_when_one_event_fails(self):
        good1, good2 = self.ev(), self.ev(subtype="pulse", payload={"value": 90})
        bad = self.ev(subtype="not_a_vital")
        with self.assertRaises(TimelineError):
            self.tl.append_many(self.enc, [good1, good2, bad])
        self.assertEqual(self.clinical(), [])
        self.assertEqual(self.audits(good1.id), [])

    def test_batch_rollback_on_persistence_error(self):
        good = self.ev()
        clash = self.ev(subtype="spo2", payload={"value": 95, "unit": "%"}, source_capture_id="cap_missing")
        with self.assertRaises(TimelineError):
            self.tl.append_many(self.enc, [good, clash])
        self.assertIsNone(self.store.get_event(good.id))

    def test_duplicate_handling(self):
        e = self.ev()
        self.tl.append(e)
        again = Event(**{**e.__dict__, "recorded_at": e.recorded_at + H})  # same facts, re-recorded
        r = self.tl.append_many(self.enc, [again, again])
        self.assertEqual((r.appended, r.duplicates), ((), (e.id,)))
        self.assertEqual(len(self.clinical()), 1)
        self.assertEqual(len(self.audits(e.id)), 1)
        changed = Event(**{**e.__dict__, "payload": {"value": 99, "unit": "F"}})
        with self.assertRaises(TimelineError):
            self.tl.append(changed)
        with self.assertRaises(TimelineError):
            self.tl.append_many(self.enc, [self.ev(id="evt_same"), self.ev(id="evt_same", payload={"value": 1})])
        self.assertIsNone(self.store.get_event("evt_same"))

    def test_extractor_rerun_is_idempotent(self):
        text = "BP 130 by 80, SpO2 97%."
        ent = {"vitals": [{"text": "BP 130 by 80", "confidence": 0.87, "source_sentence": text},
                          {"text": "SpO2 97%", "confidence": 0.87, "source_sentence": text}]}
        cap = self.store.add_capture(Capture(encounter_id=self.enc, source=CaptureSource.TEXT,
                                             capture_context=CaptureContext.WARD_ROUND, author_id="u_dr_demo",
                                             author_role=Role.CONSULTANT, captured_at=T0 + H))
        kw = dict(encounter_id=self.enc, source_capture_id=cap.id, occurred_at=T0 + H, author_id="u_dr_demo",
                  author_role=Role.CONSULTANT, source_type=SourceType.TYPED, source_text=text)
        first = self.tl.append_many(self.enc, extract_events(ent, **kw))
        second = self.tl.append_many(self.enc, extract_events(ent, **kw))  # new recorded_at, same facts
        self.assertEqual((len(first.appended), len(second.appended), len(second.duplicates)), (2, 0, 2))

    def test_invalid_and_inactive_encounter_rejected(self):
        with self.assertRaises(TimelineError):
            self.tl.append_many("enc_missing", [self.ev("enc_missing")])
        self.store.set_encounter_status(self.enc, EncounterStatus.DISCHARGED)
        with self.assertRaises(TimelineError):
            self.tl.append(self.ev())
        self.assertEqual(self.clinical(), [])  # history still readable after discharge

    def test_event_encounter_mismatch_rejected(self):
        with self.assertRaises(TimelineError):
            self.tl.append_many(self.enc, [self.ev(self.enc2)])

    def test_field_validation(self):
        base = self.ev()
        bad_versions = [
            Event(**{**base.__dict__, "id": "evt_b1", "subtype": "Temperature!"}),
            Event(**{**base.__dict__, "id": "evt_b2", "category": EventCategory.SYMPTOM, "subtype": "maybe"}),
        ]
        for b in bad_versions:
            with self.subTest(subtype=b.subtype):
                with self.assertRaises(TimelineError):
                    self.tl.append(b)
        forged = self.ev()
        object.__setattr__(forged, "confidence", 3.0)  # bypass model validation
        with self.assertRaises(TimelineError):
            self.tl.append(forged)
        forged2 = self.ev()
        object.__setattr__(forged2, "occurred_at", datetime(2026, 10, 6, 9, 0))  # naive
        with self.assertRaises(TimelineError):
            self.tl.append(forged2)
        with self.assertRaises(TimelineError):
            self.tl.append({"not": "an event"})
        self.assertEqual(self.clinical(), [])

    def test_source_capture_from_other_encounter_rejected(self):
        cap2 = self.store.add_capture(Capture(encounter_id=self.enc2, source=CaptureSource.TEXT,
                                              capture_context=CaptureContext.WARD_ROUND, author_id="u_dr_demo",
                                              author_role=Role.CONSULTANT, captured_at=T0))
        with self.assertRaises(TimelineError):
            self.tl.append(self.ev(source_capture_id=cap2.id))
        ok = self.ev(self.enc2, source_capture_id=cap2.id)
        self.assertEqual(self.tl.append(ok).appended, (ok,))


class TestAudit(TimelineBase):
    def test_audit_for_append(self):
        e = self.ev()
        self.tl.append(e)
        (a,) = self.audits(e.id)
        self.assertEqual((a.action, a.entity_id, a.user_id, a.role), (AuditAction.CREATE, e.id, "u_nurse_demo", Role.WARD_NURSE))
        self.assertEqual(a.detail["encounter_id"], self.enc)
        self.assertEqual(a.at, e.recorded_at)
        self.assertIsNotNone(a.at.tzinfo)

    def test_audit_for_correction(self):
        e1 = self.ev()
        self.tl.append(e1)
        e2 = self.tl.correct(e1.id, payload={"value": 100.1, "unit": "F"}, reason="misheard",
                             author_id="u_dr_demo", author_role=Role.CONSULTANT)
        (a,) = self.audits(e2.id)
        self.assertEqual((a.action, a.user_id, a.detail["supersedes_event_id"], a.detail["reason"],
                          a.detail["encounter_id"]), (AuditAction.SUPERSEDE, "u_dr_demo", e1.id, "misheard", self.enc))

    def test_audit_for_retraction(self):
        e1 = self.ev()
        self.tl.append(e1)
        r = self.tl.retract(e1.id, reason="duplicate entry", author_id="u_dr_demo", author_role=Role.CONSULTANT)
        (a,) = self.audits(r.id)
        self.assertEqual((a.action, a.detail["retracts_event_id"], a.detail["reason"], a.detail["encounter_id"]),
                         (AuditAction.RETRACT, e1.id, "duplicate entry", self.enc))


class TestConflicts(TimelineBase):
    def test_conflicting_vitals_detected_not_resolved(self):
        spoken = self.ev(payload={"value": 101, "unit": "F"})
        other = Event(encounter_id=self.enc, occurred_at=T0 + H, category=EventCategory.VITAL, subtype="temperature",
                      payload={"value": 99.2, "unit": "F"}, author_id="dev_monitor", author_role=Role.DEVICE,
                      source_type=SourceType.DEVICE, verification=Verification.AUTO, confidence=1.0)
        self.tl.append_many(self.enc, [spoken, other])
        (c,) = self.tl.detect_conflicts(self.enc)
        self.assertEqual((c.category, c.key, c.event_ids), (EventCategory.VITAL, "temperature", (spoken.id, other.id)))
        self.assertEqual(len(set(c.values)), 2)
        # not resolved: both stay active, nothing written
        n_events = len(self.tl.history(self.enc))
        active_ids = [e.id for e in self.tl.active_events(self.enc)]
        self.assertIn(spoken.id, active_ids)
        self.assertIn(other.id, active_ids)
        self.tl.detect_conflicts(self.enc)
        self.assertEqual(len(self.tl.history(self.enc)), n_events)
        self.assertEqual((self.tl.status_of(spoken.id), self.tl.status_of(other.id)), ("active", "active"))

    def test_symptom_present_vs_denied_conflict(self):
        p = self.ev(category=EventCategory.SYMPTOM, subtype="present", payload={"term": "fever", "negated": False})
        d = self.ev(category=EventCategory.SYMPTOM, subtype="denied", payload={"term": "fever", "negated": True})
        self.tl.append_many(self.enc, [p, d])
        (c,) = self.tl.detect_conflicts(self.enc)
        self.assertEqual((c.key, c.values), ("fever", ("present", "denied")))

    def test_no_conflict_cases(self):
        same_a = self.ev(payload={"value": 101, "unit": "F", "text": "101 F"})
        same_b = self.ev(payload={"value": 101, "unit": "F", "text": "temp 101 F"})  # same value, other wording
        later = self.ev(at=T0 + 3 * H, payload={"value": 99, "unit": "F"})  # a trend, not a conflict
        dx1 = self.ev(category=EventCategory.DIAGNOSIS, subtype="provisional", payload={"text": "pneumonia"})
        dx2 = self.ev(category=EventCategory.DIAGNOSIS, subtype="provisional", payload={"text": "sepsis"})
        self.tl.append_many(self.enc, [same_a, same_b, later, dx1, dx2])
        self.assertEqual(self.tl.detect_conflicts(self.enc), [])
        # a window wide enough to join the later reading does flag it
        self.assertEqual(len(self.tl.detect_conflicts(self.enc, window=3 * H)), 1)

    def test_corrected_value_clears_conflict(self):
        a = self.ev(payload={"value": 101, "unit": "F"})
        b = self.ev(payload={"value": 99.2, "unit": "F"})
        self.tl.append_many(self.enc, [a, b])
        self.assertEqual(len(self.tl.detect_conflicts(self.enc)), 1)
        self.tl.retract(b.id, reason="nurse confirmed 101 F", author_id="u_nurse_demo", author_role=Role.WARD_NURSE)
        self.assertEqual(self.tl.detect_conflicts(self.enc), [])  # only a human action removed it

    def test_deterministic(self):
        batch = [self.ev(payload={"value": 101, "unit": "F"}), self.ev(payload={"value": 99, "unit": "F"})]
        self.tl.append_many(self.enc, batch)
        self.assertEqual(self.tl.detect_conflicts(self.enc), self.tl.detect_conflicts(self.enc))
        self.assertEqual(self.tl.history(self.enc), self.tl.history(self.enc))


if __name__ == "__main__":
    unittest.main()
