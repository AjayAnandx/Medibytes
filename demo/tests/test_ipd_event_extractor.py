"""IPD P1 step 3: entities_json -> Event adapter (pure, deterministic).

Run:  python -m unittest demo/tests/test_ipd_event_extractor.py -v   (from repo root)
All data is synthetic. No LLM, no database file (one in-memory store check).
"""
import copy
import os
import sys
import unittest
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO = os.path.dirname(_HERE)
sys.path.insert(0, _DEMO)

from ipd import event_extractor as ex  # noqa: E402
from ipd.event_extractor import (  # noqa: E402
    DEFAULT_CONFIDENCE, extract_events, extract_events_for_capture, extract_events_with_report, parse_vital,
)
from ipd.models import (  # noqa: E402
    IST, Capture, CaptureContext, CaptureSource, EventCategory, Role, SourceRecord, SourceType, Verification,
)

T0 = datetime(2026, 10, 6, 10, 0, tzinfo=IST)
REC = T0 + timedelta(seconds=5)

S1 = "Patient has fever and cough since 3 days."
S2 = "BP 130 by 80, pulse 88, SpO2 97% on room air, temperature 101 degree."
S3 = "Patient denies chest pain."
S4 = "Start azithromycin 500 mg once daily for 3 days."
S5 = "No penicillin allergy."
S6 = "Diagnosis: community acquired pneumonia"
S7 = "Review after 3 days"
TEXT = " ".join([S1, S2, S3, S4, S5, S6 + ".", S7 + "."])
SEGMENTS = []
_t = 0.0
for _i, _s in enumerate([S1, S2, S3, S4, S5, S6 + ".", S7 + "."]):
    SEGMENTS.append({"id": _i, "text": _s, "start": round(_t, 2), "end": round(_t + 2.5, 2)})
    _t += 2.7

ENTITIES = {
    "job_id": "synthetic",
    "symptoms": [
        {"text": "fever", "confidence": 0.9, "source_sentence": S1, "negated": False},
        {"text": "cough", "confidence": 0.9, "source_sentence": S1, "negated": False},
        {"text": "chest pain", "confidence": 0.91, "source_sentence": S3, "negated": True, "note": "DENIED - excluded"},
    ],
    "vitals": [
        {"text": "BP 130 by 80", "confidence": 0.87, "source_sentence": S2},
        {"text": "pulse 88", "confidence": 0.87, "source_sentence": S2},
        {"text": "SpO2 97%", "confidence": 0.87, "source_sentence": S2},
        {"text": "101 degree", "confidence": 0.87, "source_sentence": S2},
    ],
    "drugs": [{"name": "azithromycin", "dose": 500.0, "unit": "mg", "frequency": "once daily",
               "duration": "3 days", "confidence": 0.96, "source_sentence": S4, "negated": False}],
    "allergies": [{"text": "penicillin", "negated": True, "confidence": 0.9, "source_sentence": S5}],
    "negations": [{"span": S5, "negated": True}],
    "diagnosis": {"text": "community acquired pneumonia", "icd10": "", "confidence": 0.85, "source_sentence": S6},
    "followup": {"text": "Review after 3 days", "confidence": 0.88, "source_sentence": S7},
}


def run(entities=None, **kw):
    args = dict(encounter_id="enc_demo", source_capture_id="cap_demo", occurred_at=T0, author_id="u_dr_demo",
                author_role=Role.CONSULTANT, source_type=SourceType.SPOKEN, source_text=TEXT,
                segments=SEGMENTS, recorded_at=REC)
    args.update(kw)
    return extract_events_with_report(ENTITIES if entities is None else entities, **args)


def by(events, category, subtype=None):
    return [e for e in events if e.category == category and (subtype is None or e.subtype == subtype)]


class TestMappings(unittest.TestCase):
    def setUp(self):
        self.events, self.skipped = run()

    def test_positive_symptom(self):
        present = by(self.events, EventCategory.SYMPTOM, "present")
        self.assertEqual([e.payload["term"] for e in present], ["fever", "cough"])
        self.assertFalse(present[0].payload["negated"])

    def test_negated_symptom(self):
        denied = by(self.events, EventCategory.SYMPTOM, "denied")
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0].payload["term"], "chest pain")
        self.assertTrue(denied[0].payload["negated"])
        self.assertEqual(denied[0].payload["extractor_note"], "DENIED - excluded")

    def test_bp(self):
        (bp,) = by(self.events, EventCategory.VITAL, "bp")
        self.assertEqual((bp.payload["systolic"], bp.payload["diastolic"]), (130, 80))
        self.assertEqual(bp.payload["text"], "BP 130 by 80")

    def test_temperature_unit_not_invented(self):
        (t,) = by(self.events, EventCategory.VITAL, "temperature")
        self.assertEqual(t.payload["value"], 101)
        self.assertIsNone(t.payload["unit"])  # "101 degree" states no F/C

    def test_spo2(self):
        (s,) = by(self.events, EventCategory.VITAL, "spo2")
        self.assertEqual((s.payload["value"], s.payload["unit"]), (97, "%"))

    def test_pulse(self):
        (p,) = by(self.events, EventCategory.VITAL, "pulse")
        self.assertEqual(p.payload["value"], 88)

    def test_diagnosis(self):
        (d,) = by(self.events, EventCategory.DIAGNOSIS)
        self.assertEqual(d.subtype, "provisional")
        self.assertEqual(d.payload["text"], "community acquired pneumonia")
        self.assertNotIn("icd10", d.payload)  # empty code is not a fact

    def test_medication_is_proposed_and_unverified(self):
        (m,) = by(self.events, EventCategory.MEDICATION_ORDER)
        self.assertEqual(m.subtype, "proposed")
        self.assertEqual(m.verification, Verification.UNVERIFIED)
        self.assertEqual({k: m.payload[k] for k in ("name", "dose", "unit", "frequency", "duration")},
                         {"name": "azithromycin", "dose": 500.0, "unit": "mg", "frequency": "once daily",
                          "duration": "3 days"})
        self.assertNotIn("missing", m.payload)
        self.assertNotIn("route", m.payload)  # not extracted -> not invented

    def test_allergy(self):
        (a,) = by(self.events, EventCategory.ALLERGY)
        self.assertEqual((a.subtype, a.payload["substance"], a.payload["negated"]), ("denied", "penicillin", True))
        active, _ = run({"allergies": [{"text": "sulfa", "negated": False, "confidence": 0.9, "source_sentence": ""}]})
        self.assertEqual(active[0].subtype, "active")

    def test_followup_is_plan(self):
        (p,) = by(self.events, EventCategory.PLAN)
        self.assertEqual((p.subtype, p.payload["text"]), ("followup", "Review after 3 days"))

    def test_all_events_unverified_and_context_applied(self):
        self.assertEqual(len(self.events), 11)
        for e in self.events:
            self.assertEqual(e.verification, Verification.UNVERIFIED)
            self.assertEqual((e.encounter_id, e.source_capture_id, e.author_id, e.author_role, e.source_type),
                             ("enc_demo", "cap_demo", "u_dr_demo", Role.CONSULTANT, SourceType.SPOKEN))
            self.assertEqual((e.occurred_at, e.recorded_at), (T0, REC))
        self.assertEqual(self.skipped, [])  # negations/job_id ignored silently


class TestConfidenceAndProvenance(unittest.TestCase):
    def test_confidence_preserved(self):
        events, _ = run()
        conf = {(e.category, e.subtype, e.payload.get("term") or e.payload.get("name") or ""): e.confidence
                for e in events}
        self.assertEqual(conf[(EventCategory.SYMPTOM, "denied", "chest pain")], 0.91)
        self.assertEqual(conf[(EventCategory.MEDICATION_ORDER, "proposed", "azithromycin")], 0.96)
        self.assertEqual(by(events, EventCategory.DIAGNOSIS)[0].confidence, 0.85)

    def test_invalid_confidence_rejected(self):
        for bad in (1.5, -0.1, "0.9", True, float("nan")):
            with self.subTest(bad=bad):
                events, skipped = run({"symptoms": [{"text": "fever", "confidence": bad, "source_sentence": S1}]})
                self.assertEqual(events, [])
                self.assertIn("invalid confidence", skipped[0]["reason"])

    def test_missing_confidence_uses_flagged_default(self):
        events, _ = run({"followup": {"text": "Review after 3 days", "source_sentence": S7}})
        self.assertEqual(events[0].confidence, DEFAULT_CONFIDENCE)
        self.assertEqual(events[0].payload["confidence_source"], "default")

    def test_source_sentence_preserved(self):
        events, _ = run()
        for e in events:
            self.assertTrue(e.payload["source_sentence"])
            self.assertIn(e.payload["source_sentence"], TEXT)

    def test_char_span(self):
        events, _ = run()
        (m,) = by(events, EventCategory.MEDICATION_ORDER)
        span = m.source_span
        self.assertEqual(TEXT[span["char_start"]:span["char_end"]], S4)
        self.assertTrue(m.payload["evidence_located"])

    def test_segment_provenance(self):
        events, _ = run()
        (m,) = by(events, EventCategory.MEDICATION_ORDER)
        seg = SEGMENTS[3]
        self.assertEqual(m.source_span["segment_id"], 3)
        self.assertEqual((m.source_span["start_ms"], m.source_span["end_ms"]),
                         (int(round(seg["start"] * 1000)), int(round(seg["end"] * 1000))))
        self.assertNotIn("ocr_line", m.source_span)

    def test_case_and_whitespace_insensitive_location(self):
        text = "patient  DENIES\nchest pain."
        events, _ = run({"symptoms": [{"text": "chest pain", "negated": True, "confidence": 0.9,
                                       "source_sentence": "Patient denies chest pain."}]},
                        source_text=text, segments=None)
        span = events[0].source_span
        self.assertEqual(text[span["char_start"]:span["char_end"]], "patient  DENIES\nchest pain.")
        self.assertNotIn("segment_id", span)  # no segments supplied -> none invented

    def test_missing_evidence_not_fabricated(self):
        ent = {"symptoms": [{"text": "headache", "confidence": 0.8, "source_sentence": "Complains of headache."}],
               "diagnosis": {"text": "migraine", "confidence": 0.8, "source_sentence": ""}}
        events, _ = run(ent)
        head = by(events, EventCategory.SYMPTOM)[0]
        self.assertEqual(head.source_span, {})
        self.assertFalse(head.payload["evidence_located"])
        self.assertEqual(head.payload["source_sentence"], "Complains of headache.")
        self.assertEqual(head.verification, Verification.UNVERIFIED)
        dx = by(events, EventCategory.DIAGNOSIS)[0]
        self.assertEqual(dx.source_span, {})
        self.assertNotIn("source_sentence", dx.payload)
        self.assertFalse(dx.payload["evidence_located"])

    def test_ambiguous_evidence_flagged(self):
        text = "No fever. No fever."
        events, _ = run({"symptoms": [{"text": "fever", "negated": True, "confidence": 0.9,
                                       "source_sentence": "No fever."}]}, source_text=text, segments=None)
        self.assertEqual(events[0].source_span["char_start"], 0)
        self.assertTrue(events[0].payload["evidence_ambiguous"])

    def test_document_source_uses_ocr_line_not_fake_timing(self):
        lines = ["Patient: Demo Patient", "Temperature: 101 F", "Paracetamol 500 mg"]
        segs = [{"id": i, "text": ln, "start": 0.0, "end": 0.0} for i, ln in enumerate(lines)]
        ent = {"vitals": [{"text": "Temperature: 101 F", "confidence": 0.87, "source_sentence": "Temperature: 101 F"}]}
        events, _ = run(ent, source_text="\n".join(lines), segments=segs, source_type=SourceType.DOCUMENT)
        span = events[0].source_span
        self.assertEqual((span["segment_id"], span["ocr_line"]), (1, 1))
        self.assertNotIn("start_ms", span)
        self.assertEqual((events[0].payload["value"], events[0].payload["unit"]), (101, "F"))


class TestDedupeDeterminismSafety(unittest.TestCase):
    def test_duplicate_vital_structured_and_free_text(self):
        ent = copy.deepcopy(ENTITIES)
        ent["structured_vitals"] = {"sys": "130", "dia": "80", "spo2": "97", "temp": "101"}
        events, skipped = run(ent)
        self.assertEqual(len(by(events, EventCategory.VITAL, "bp")), 1)
        self.assertEqual(len(by(events, EventCategory.VITAL, "spo2")), 1)
        self.assertEqual(len(by(events, EventCategory.VITAL, "temperature")), 1)
        self.assertTrue(by(events, EventCategory.VITAL, "bp")[0].payload["evidence_located"])  # evidenced one kept
        self.assertEqual(sum(1 for s in skipped if s["reason"].startswith("duplicate")), 3)

    def test_structured_only_vital_has_no_invented_span(self):
        events, _ = run({"structured_vitals": {"sys": "118", "dia": "76"}})
        (bp,) = events
        self.assertEqual((bp.payload["systolic"], bp.payload["diastolic"], bp.payload["structured"]), (118, 76, True))
        self.assertEqual(bp.source_span, {})
        self.assertEqual(bp.payload["confidence_source"], "default")

    def test_incomplete_structured_bp_not_mapped(self):
        events, skipped = run({"structured_vitals": {"sys": "130", "dia": ""}})
        self.assertEqual(events, [])
        self.assertEqual(skipped[0]["field"], "structured_vitals.bp")

    def test_different_values_are_not_merged(self):
        ent = {"vitals": [{"text": "SpO2 97%", "confidence": 0.87, "source_sentence": S2}],
               "structured_vitals": {"spo2": "92"}}
        events, _ = run(ent)
        self.assertEqual(sorted(e.payload["value"] for e in by(events, EventCategory.VITAL, "spo2")), [92, 97])

    def test_hinglish_synonym_duplicate_symptom(self):
        s = "Patient ko fever hai, bukhar 101 degree."
        events, skipped = run({"symptoms": [{"text": "fever", "confidence": 0.9, "source_sentence": s},
                                            {"text": "bukhar", "confidence": 0.9, "source_sentence": s}]},
                              source_text=s, segments=None)
        self.assertEqual([e.payload["term"] for e in events], ["fever"])
        self.assertEqual(skipped[0]["reason"], "duplicate of symptoms[0]")

    def test_denied_and_present_are_distinct(self):
        events, _ = run({"symptoms": [{"text": "fever", "negated": False, "confidence": 0.9, "source_sentence": S1},
                                      {"text": "fever", "negated": True, "confidence": 0.9, "source_sentence": S3}]})
        self.assertEqual(sorted(e.subtype for e in events), ["denied", "present"])

    def test_deterministic(self):
        a, sa = run()
        b, sb = run(copy.deepcopy(ENTITIES))
        self.assertEqual(a, b)
        self.assertEqual(sa, sb)
        self.assertEqual(len({e.id for e in a}), len(a))
        c, _ = run(source_capture_id="cap_other")
        self.assertTrue({e.id for e in a}.isdisjoint({e.id for e in c}))

    def test_empty_entities_produce_no_events(self):
        for ent in ({}, {"symptoms": [], "vitals": [], "drugs": [], "allergies": [], "diagnosis": {}, "followup": {},
                         "negations": [], "structured_vitals": {}},
                    {"job_id": "x", "llm_engine": "regex-only", "patient": {"name": "Demo Patient"}}):
            with self.subTest(ent=ent):
                events, skipped = run(ent)
                self.assertEqual((events, skipped), ([], []))

    def test_missing_fields_never_become_facts(self):
        events, _ = run({"drugs": [{"name": "paracetamol", "dose": None, "unit": None, "frequency": "",
                                    "duration": "", "confidence": 0.7, "source_sentence": ""}]})
        (m,) = events
        self.assertEqual(m.payload["name"], "paracetamol")
        for k in ("dose", "unit", "frequency", "duration"):
            self.assertNotIn(k, m.payload)
        self.assertEqual(m.payload["missing"], ["dose", "unit", "frequency", "duration"])

    def test_malformed_and_unsupported_handled_safely(self):
        ent = {"symptoms": ["fever", {"text": ""}, {"no_text": 1}, None],
               "vitals": [{"text": "looks comfortable"}, 42, {"text": ""}],
               "drugs": [{"dose": 500}, {"name": "ibuprofen", "negated": True, "confidence": 0.9}],
               "allergies": [{"negated": True}],
               "diagnosis": "pneumonia", "followup": ["review"], "structured_vitals": "130/80",
               "unknown_key": {"anything": True}}
        events, skipped = run(ent)
        self.assertEqual(events, [])
        # 4 symptoms + 3 vitals + 2 drugs + 1 allergy + diagnosis + followup + structured_vitals;
        # unknown_key is ignored, not reported
        self.assertEqual(len(skipped), 13)
        for s in skipped:
            self.assertTrue(s["field"] and s["reason"])
        with self.assertRaises(ValueError):
            run("not a dict")
        with self.assertRaises(ValueError):
            run({}, source_capture_id="")

    def test_naive_occurred_at_rejected(self):
        with self.assertRaises(ValueError):
            run({"followup": {"text": "Review", "confidence": 0.9}}, occurred_at=datetime(2026, 10, 6, 10, 0))

    def test_no_llm_or_io_in_module(self):
        with open(ex.__file__, encoding="utf-8") as fh:
            src = fh.read()
        for banned in ("ollama", "subprocess", "sqlite3", "open(", "requests", "stt_extract", "llm_extract"):
            self.assertNotIn(banned, src)


class TestParseVital(unittest.TestCase):
    def test_parse_table(self):
        cases = {
            "BP 120/80": ("bp", {"systolic": 120, "diastolic": 80}),
            "blood pressure 110 over 70": ("bp", {"systolic": 110, "diastolic": 70}),
            "Temperature: 101 F": ("temperature", {"value": 101, "unit": "F"}),
            "temp 37.5 C": ("temperature", {"value": 37.5, "unit": "C"}),
            "99.4 degrees fahrenheit": ("temperature", {"value": 99.4, "unit": "F"}),
            "98 percent on room air": ("spo2", {"value": 98, "unit": "%"}),
            "heart rate 112": ("pulse", {"value": 112}),
            "HR 64": ("pulse", {"value": 64}),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_vital(text), expected)
        for text in ("", "looks comfortable", "RR 18"):
            self.assertIsNone(parse_vital(text))


class TestWithCaptureAndPipeline(unittest.TestCase):
    def test_for_capture_convenience(self):
        cap = Capture(encounter_id="enc_demo", source=CaptureSource.TEXT, capture_context=CaptureContext.WARD_ROUND,
                      author_id="u_res_demo", author_role=Role.RESIDENT, captured_at=T0, id="cap_typed")
        src = SourceRecord(capture_id=cap.id, text=TEXT, segments=SEGMENTS)
        events, _ = extract_events_for_capture(ENTITIES, cap, src, recorded_at=REC)
        self.assertEqual(len(events), 11)
        self.assertTrue(all(e.source_type == SourceType.TYPED and e.author_role == Role.RESIDENT
                            and e.source_capture_id == "cap_typed" for e in events))

    def test_real_regex_pipeline_output_and_storable(self):
        """Existing extractor (regex only, no LLM) -> adapter -> in-memory store accepts the events."""
        from stt_extract import run_text_extract
        from ipd.encounter import admit_patient, synthetic_patient
        from ipd.store import IpdStore
        text = ("Khansi hai, cough for 5 days, BP 130 by 80, pulse 88, SpO2 97% on room air, give azithromycin "
                "500 mg once daily 3 days. No penicillin allergy, patient denies chest pain. "
                "Diagnosis: community acquired pneumonia. Review after 3 days.")
        segs = [{"id": 0, "text": text, "start": 0.0, "end": 9.0, "lang": "", "confidence": 0.9, "words": []}]
        _, ej = run_text_extract(text, segs, use_llm=False)
        with IpdStore() as store:
            adm = admit_patient(store, synthetic_patient("E01"), ward="Ward 4", bed="1",
                                admitted_by="u_dr_demo", admitted_role=Role.CONSULTANT, admit_at=T0)
            events = extract_events(ej, encounter_id=adm.encounter.id, source_capture_id="cap_pipe",
                                    occurred_at=T0, author_id="u_dr_demo", author_role=Role.CONSULTANT,
                                    source_type=SourceType.SPOKEN, source_text=text, segments=segs, recorded_at=REC)
            kinds = sorted((e.category.value, e.subtype) for e in events)
            self.assertEqual(kinds, sorted([
                ("SYMPTOM", "present"), ("SYMPTOM", "denied"), ("VITAL", "bp"), ("VITAL", "pulse"),
                ("VITAL", "spo2"), ("MEDICATION_ORDER", "proposed"), ("ALLERGY", "denied"),
                ("DIAGNOSIS", "provisional"), ("PLAN", "followup")]))  # khansi/cough -> one SYMPTOM
            store._conn.execute("INSERT INTO captures (id, encounter_id, source, capture_context, author_id, "
                                "author_role, location, captured_at, raw_uri, status, pipeline_info) VALUES "
                                "('cap_pipe', ?, 'audio', 'ward_round', 'u_dr_demo', 'CONSULTANT', '{}', ?, NULL, "
                                "'processed', '{}')", (adm.encounter.id, T0.isoformat()))
            for e in events:
                store.append_event(e)
            self.assertEqual(len(store.list_events(adm.encounter.id)), len(events) + 1)  # + ADT admission


if __name__ == "__main__":
    unittest.main()
