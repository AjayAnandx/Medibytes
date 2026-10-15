"""IPD P1 step 5A: typed ward-round capture -> extractor -> events -> timeline.

Run:  python -m unittest demo/tests/test_ipd_capture.py -v   (from repo root)
In-memory SQLite, synthetic data. No LLM (Ollama entry points are trapped).
"""
import hashlib
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO = os.path.dirname(_HERE)
sys.path.insert(0, _DEMO)

import llm_extract  # noqa: E402
import stt_extract  # noqa: E402
from ipd import capture as cap_mod  # noqa: E402
from ipd.capture import (  # noqa: E402
    EXTRACTOR_SOURCE_MODE, CaptureError, CaptureProcessingError, capture_typed_ward_round, reprocess_capture,
)
from ipd.encounter import admit_patient, synthetic_patient  # noqa: E402
from ipd.models import (  # noqa: E402
    IST, AuditAction, CaptureContext, CaptureSource, CaptureStatus, EncounterStatus, EventCategory, Role,
    SourceType, Verification,
)
from ipd.store import IpdStore  # noqa: E402
from ipd.timeline import Timeline, TimelineError  # noqa: E402

T0 = datetime(2026, 10, 6, 8, 0, tzinfo=IST)
ROUND_AT = T0 + timedelta(hours=2)
LOC = {"ward": "Ward 4", "bed": "12", "unit_type": "WARD"}
NOTE = ("Patient has fever. No chest pain.\n"
        "BP 130/80, pulse 88, SpO2 96%.\n"
        "Temperature: 101 F\n"
        "Diagnosis: viral fever.\n"
        "Start paracetamol 650 mg TDS.")


def _boom(*a, **k):
    raise AssertionError("LLM/Ollama must not be called on the typed-text path")


class CaptureBase(unittest.TestCase):
    def setUp(self):
        self.store = IpdStore()
        self.adm = admit_patient(self.store, synthetic_patient("C01"), ward="Ward 4", bed="12",
                                 admitted_by="u_dr_demo", admitted_role=Role.CONSULTANT, admit_at=T0)
        self.enc = self.adm.encounter.id
        self.tl = Timeline(self.store)
        patches = [mock.patch.object(llm_extract, "ollama_available", _boom),
                   mock.patch.object(llm_extract, "extract_llm_primary", _boom),
                   mock.patch.object(stt_extract, "ollama_tidy", _boom),
                   mock.patch("subprocess.run", _boom)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.store.close()

    def capture(self, text=NOTE, enc=None, **kw):
        args = dict(encounter_id=enc or self.enc, text=text, author_id="u_dr_demo", author_role=Role.CONSULTANT,
                    location=LOC, captured_at=ROUND_AT)
        args.update(kw)
        return capture_typed_ward_round(self.store, **args)

    def clinical(self, enc=None, active=False):
        evs = self.tl.history(enc or self.enc, active_only=active)
        return [e for e in evs if e.category != EventCategory.ADT]

    def count(self, table):
        return self.store._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class TestSuccessfulCapture(CaptureBase):
    def setUp(self):
        super().setUp()
        self.r = self.capture()

    def test_success_and_capture_context(self):
        r, c = self.r, self.r.capture
        self.assertTrue(r.ok)
        self.assertEqual(c.status, CaptureStatus.PROCESSED)
        self.assertEqual(self.store.get_capture(c.id), c)
        self.assertEqual((c.encounter_id, c.author_id, c.author_role, c.location, c.captured_at),
                         (self.enc, "u_dr_demo", Role.CONSULTANT, LOC, ROUND_AT))
        self.assertEqual(c.source, CaptureSource.TEXT)            # never "image"
        self.assertEqual(c.capture_context, CaptureContext.WARD_ROUND)
        self.assertEqual(c.pipeline_info["extractor_source_mode"], "image")
        self.assertFalse(c.pipeline_info["use_llm"])
        self.assertEqual(c.pipeline_info["n_appended"], len(r.appended_event_ids))

    def test_source_record(self):
        s = self.store.get_source_record(self.r.capture.id)
        self.assertEqual(s, self.r.source_record)
        self.assertEqual(s.text, NOTE)
        self.assertTrue(s.normalized_text)
        self.assertIn("typed_text", s.engine)
        self.assertEqual([seg["text"] for seg in s.segments], NOTE.split("\n"))
        for seg in s.segments:  # no fake audio timing
            self.assertNotIn("start", seg)
            self.assertNotIn("end", seg)
        for e in self.clinical():
            self.assertNotIn("start_ms", e.source_span)
            self.assertNotIn("end_ms", e.source_span)

    def test_events_reach_timeline(self):
        evs = self.clinical()
        self.assertEqual({e.id for e in evs}, set(self.r.appended_event_ids))
        kinds = {(e.category, e.subtype) for e in evs}
        present = [e for e in evs if e.category == EventCategory.SYMPTOM and e.subtype == "present"]
        denied = [e for e in evs if e.category == EventCategory.SYMPTOM and e.subtype == "denied"]
        self.assertEqual([e.payload["term"] for e in present], ["fever"])     # "viral fever" duplicate collapsed
        self.assertEqual([(e.payload["term"], e.payload["negated"]) for e in denied], [("chest pain", True)])
        for sub in ("bp", "pulse", "spo2", "temperature"):
            self.assertIn((EventCategory.VITAL, sub), kinds)
        temp = next(e for e in evs if e.subtype == "temperature")
        self.assertEqual((temp.payload["value"], temp.payload["unit"]), (101, "F"))
        dx = next(e for e in evs if e.category == EventCategory.DIAGNOSIS)
        self.assertEqual((dx.subtype, dx.payload["text"]), ("provisional", "viral fever"))

    def test_medication_stays_proposed_unverified(self):
        meds = [e for e in self.clinical() if e.category == EventCategory.MEDICATION_ORDER]
        self.assertEqual([(m.subtype, m.verification, m.payload["name"], m.payload["dose"]) for m in meds],
                         [("proposed", Verification.UNVERIFIED, "paracetamol", 650.0)])
        self.assertEqual(meds[0].payload["frequency"], "TDS")

    def test_provenance_author_confidence(self):
        for e in self.clinical():
            self.assertEqual((e.source_capture_id, e.source_type, e.author_id, e.author_role, e.occurred_at),
                             (self.r.capture.id, SourceType.TYPED, "u_dr_demo", Role.CONSULTANT, ROUND_AT))
            self.assertEqual(e.verification, Verification.UNVERIFIED)
            self.assertTrue(e.payload["evidence_located"])
            span = e.source_span
            self.assertEqual(NOTE[span["char_start"]:span["char_end"]], e.payload["source_sentence"])
            self.assertIn("segment_id", span)
            self.assertTrue(0.0 < e.confidence <= 1.0)
        med = next(e for e in self.clinical() if e.category == EventCategory.MEDICATION_ORDER)
        self.assertEqual(med.source_span["segment_id"], 4)
        self.assertEqual(med.confidence, self.r.entities["drugs"][0]["confidence"])

    def test_extractor_called_deterministically_without_llm(self):
        with mock.patch.object(stt_extract, "run_text_extract", wraps=stt_extract.run_text_extract) as spy:
            self.capture(text="Patient denies chest pain.")
        kw = spy.call_args.kwargs
        self.assertEqual((kw["use_llm"], kw["source"]), (False, EXTRACTOR_SOURCE_MODE))
        self.assertIn("regex-only", self.r.entities.get("llm_engine", ""))
        self.assertNotIn("ai_eval", self.r.entities)

    def test_audit(self):
        (a,) = self.store.list_audit("capture", self.r.capture.id)
        self.assertEqual((a.action, a.user_id, a.detail["status"], a.detail["encounter_id"]),
                         (AuditAction.CREATE, "u_dr_demo", "processed", self.enc))
        for eid in self.r.appended_event_ids:
            self.assertEqual(len(self.store.list_audit("event", eid)), 1)

    def test_no_conflict_auto_resolution(self):
        self.assertEqual(self.tl.detect_conflicts(self.enc), [])
        self.assertEqual(len(self.clinical(active=True)), len(self.clinical()))


class TestIsolationAndRejection(CaptureBase):
    def test_encounter_isolation(self):
        other = admit_patient(self.store, synthetic_patient("C02"), ward="Ward 5", bed="1",
                              admitted_by="u_dr_demo", admitted_role=Role.CONSULTANT, admit_at=T0)
        r1 = self.capture(text="Patient has fever.")
        r2 = self.capture(text="Patient denies chest pain.", enc=other.encounter.id)
        self.assertEqual({e.id for e in self.clinical()}, set(r1.appended_event_ids))
        self.assertEqual({e.id for e in self.clinical(other.encounter.id)}, set(r2.appended_event_ids))
        self.assertTrue(set(r1.appended_event_ids).isdisjoint(r2.appended_event_ids))

    def _assert_rejected_nothing_written(self, **kw):
        before = {t: self.count(t) for t in ("captures", "source_records", "events", "audit_log")}
        with self.assertRaises(CaptureError):
            self.capture(**kw)
        self.assertEqual({t: self.count(t) for t in before}, before)

    def test_invalid_encounter_rejected(self):
        self._assert_rejected_nothing_written(enc="enc_missing")

    def test_discharged_encounter_rejected(self):
        self.store.set_encounter_status(self.enc, EncounterStatus.DISCHARGED)
        self._assert_rejected_nothing_written()

    def test_empty_or_invalid_input_rejected(self):
        for kw in (dict(text=""), dict(text="   \n  "), dict(text=None), dict(text="x" * 20001),
                   dict(author_id=""), dict(author_role=Role.WARD_NURSE), dict(location={}),
                   dict(location={"ward": "Ward 4"}), dict(captured_at=datetime(2026, 10, 6, 10, 0))):
            with self.subTest(**{k: str(v)[:20] for k, v in kw.items()}):
                self._assert_rejected_nothing_written(**kw)

    def test_text_without_clinical_content_is_processed_with_no_events(self):
        r = self.capture(text="Seen on round. Plan discussed with family.")
        self.assertTrue(r.ok)
        self.assertEqual((r.appended_event_ids, self.clinical()), ((), []))


class TestFailures(CaptureBase):
    def _assert_failed(self, ctx, expect_source=True):
        with self.assertRaises(CaptureProcessingError) as cm:
            self.capture()
        r = cm.exception.result
        self.assertFalse(r.ok)
        self.assertEqual(r.status, CaptureStatus.FAILED)
        stored = self.store.get_capture(r.capture.id)
        self.assertEqual(stored.status, CaptureStatus.FAILED)       # never PROCESSED
        self.assertIn("error", stored.pipeline_info)
        self.assertEqual(self.clinical(), [])                       # no partial clinical state
        self.assertEqual(self.store.list_events(self.enc)[0].category, EventCategory.ADT)  # history intact
        self.assertEqual(self.store.get_source_record(r.capture.id) is not None, expect_source)
        (a,) = self.store.list_audit("capture", r.capture.id)
        self.assertEqual(a.detail["status"], "failed")
        return r

    def test_extraction_failure(self):
        with mock.patch.object(stt_extract, "run_text_extract", side_effect=RuntimeError("extractor crashed")):
            r = self._assert_failed(None)
        self.assertIn("extraction failed", r.error)

    def test_event_conversion_failure(self):
        with mock.patch.object(cap_mod, "extract_events_for_capture", side_effect=ValueError("bad entities")):
            self._assert_failed(None)

    def test_timeline_append_failure(self):
        with mock.patch.object(Timeline, "append_many", side_effect=TimelineError("append failed")):
            r = self._assert_failed(None)
        self.assertIn("append failed", r.error)

    def test_failure_mid_batch_leaves_no_events(self):
        real = self.store.append_event
        calls = {"n": 0}

        def flaky(e):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("disk full")
            return real(e)
        with mock.patch.object(self.store, "append_event", side_effect=flaky):
            self._assert_failed(None)
        self.assertGreaterEqual(calls["n"], 3)

    def test_source_record_persistence_failure(self):
        with mock.patch.object(self.store, "add_source_record", side_effect=RuntimeError("cannot store text")):
            r = self._assert_failed(None, expect_source=False)
        self.assertIn("source_record_error", self.store.get_capture(r.capture.id).pipeline_info)

    def test_failed_capture_can_be_reprocessed(self):
        with mock.patch.object(Timeline, "append_many", side_effect=TimelineError("transient")):
            with self.assertRaises(CaptureProcessingError) as cm:
                self.capture()
        cid = cm.exception.result.capture.id
        r = reprocess_capture(self.store, cid)
        self.assertTrue(r.ok)
        self.assertEqual(self.store.get_capture(cid).status, CaptureStatus.PROCESSED)
        self.assertEqual({e.id for e in self.clinical()}, set(r.appended_event_ids))


class TestIdempotency(CaptureBase):
    def test_reprocess_does_not_duplicate(self):
        r1 = self.capture()
        n = len(self.clinical())
        r2 = reprocess_capture(self.store, r1.capture.id)
        self.assertTrue(r2.ok)
        self.assertEqual(r2.appended_event_ids, ())
        self.assertEqual(set(r2.duplicate_event_ids), set(r1.appended_event_ids))
        self.assertEqual(len(self.clinical()), n)
        self.assertEqual(self.store.get_capture(r1.capture.id).pipeline_info["n_duplicates"], n)

    def test_same_capture_id_resubmitted(self):
        r1 = self.capture(capture_id="cap_client_key_1")
        r2 = self.capture(capture_id="cap_client_key_1")
        self.assertEqual((r2.appended_event_ids, set(r2.duplicate_event_ids)), ((), set(r1.appended_event_ids)))
        self.assertEqual(self.count("captures"), 1)
        with self.assertRaises(CaptureError):
            self.capture(capture_id="cap_client_key_1", text="Different text.")

    def test_reprocess_failure_keeps_processed_capture(self):
        r1 = self.capture()
        with mock.patch.object(Timeline, "append_many", side_effect=TimelineError("transient")):
            with self.assertRaises(CaptureProcessingError):
                reprocess_capture(self.store, r1.capture.id)
        c = self.store.get_capture(r1.capture.id)
        self.assertEqual(c.status, CaptureStatus.PROCESSED)  # its earlier events are valid and still there
        self.assertIn("last_reprocess_error", c.pipeline_info)
        self.assertEqual({e.id for e in self.clinical()}, set(r1.appended_event_ids))


class TestErRegression(unittest.TestCase):
    def _h(self, o):
        return hashlib.sha256(json.dumps(o, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]

    def test_er_extraction_unchanged(self):
        """Audio ER outputs match the recorded baseline before and after a typed IPD capture."""
        baseline = {"demo-001": ("43c32d9c00ada83f", "5dcdbfa65b309640"),
                    "demo-002": ("496c0d18c80a7ad2", "89d94317c2ae36a8")}

        def er():
            out = {}
            for job in baseline:
                tj, ej = stt_extract.run_stt_extract(os.path.join(_DEMO, "cleaned", f"{job}.wav"), job,
                                                     use_llm=False, model="mock")
                out[job] = (self._h(tj), self._h(ej))
            return out
        self.assertEqual(er(), baseline)
        with IpdStore() as store:
            adm = admit_patient(store, synthetic_patient("R01"), ward="Ward 4", bed="1",
                                admitted_by="u_dr_demo", admitted_role=Role.CONSULTANT, admit_at=T0)
            capture_typed_ward_round(store, encounter_id=adm.encounter.id, text=NOTE, author_id="u_dr_demo",
                                     author_role=Role.CONSULTANT, location=LOC, captured_at=ROUND_AT)
        self.assertEqual(er(), baseline)

    def test_er_directories_untouched(self):
        dirs = ("entities", "exports", "transcripts", "cleaned", "_state", "templates", "assets")

        def snap():
            s = {}
            for d in dirs:
                for base, _, files in os.walk(os.path.join(_DEMO, d)):
                    for f in files:
                        st = os.stat(os.path.join(base, f))
                        s[os.path.join(base, f)] = (st.st_size, st.st_mtime_ns)
            return s
        before = snap()
        with IpdStore() as store:
            adm = admit_patient(store, synthetic_patient("R02"), ward="Ward 4", bed="1",
                                admitted_by="u_dr_demo", admitted_role=Role.CONSULTANT, admit_at=T0)
            capture_typed_ward_round(store, encounter_id=adm.encounter.id, text=NOTE, author_id="u_dr_demo",
                                     author_role=Role.CONSULTANT, location=LOC, captured_at=ROUND_AT)
        self.assertEqual(snap(), before)


if __name__ == "__main__":
    unittest.main()
