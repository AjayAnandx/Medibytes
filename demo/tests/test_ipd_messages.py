"""Regression: IPD outcome messages survive the post-write rerun; heart rate becomes an event.

Run:  python -m pytest demo/tests/test_ipd_messages.py -q

Found on 2026-10-16 with an end-to-end run of all six tabs: the Generate /
Regenerate / Approve outcome was popped before the automatic rerun, so the
confirmation (or a refusal reason) vanished immediately. Synthetic patients,
temp database only.
"""
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO = os.path.dirname(_HERE)
sys.path.insert(0, _DEMO)

from streamlit.testing.v1 import AppTest  # noqa: E402

NOTE = ("Day 2 ward round. Fever settling. Temp 99.1 degrees Fahrenheit, BP 124/80, SpO2 97% on room air, "
        "heart rate of 88. Continue paracetamol 500 mg twice daily for 3 days. Diagnosis: test pneumonia.")


class TestIpdOutcomeMessages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="medibytes_ipd_msg_")
        env = mock.patch.dict(os.environ, {"MEDIBYTES_IPD_DB": os.path.join(self.tmp, "ipd.sqlite")})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        at = self.at = AppTest.from_file(os.path.join(_DEMO, "ipd_app.py"), default_timeout=120)
        at.run()
        for k, v in (("ipd_adm_name", "Demo Patient M01"), ("ipd_adm_mrn", "DEMO-M01"), ("ipd_adm_ward", "Ward 4"),
                     ("ipd_adm_bed", "5"), ("ipd_adm_by", "u_dr_demo")):
            at.text_input(key=k).input(v)
        at.selectbox(key="ipd_adm_role").select("CONSULTANT")
        at.button(key="ipd_admit").click().run()
        at.text_area(key="ipd_round_text").input(NOTE)
        at.text_input(key="ipd_round_by").input("u_dr_demo")
        at.button(key="ipd_process").click().run()

    def _texts(self, kind):
        return [str(x.value) for x in getattr(self.at, kind)]

    def test_generate_confirmation_visible_after_rerun(self):
        self.at.text_input(key="ipd_gen_by").input("u_dr_demo")
        self.at.button(key="ipd_generate").click().run()
        self.assertEqual([str(e.value) for e in self.at.exception], [])
        self.assertTrue(any(t.startswith("Progress note draft created") for t in self._texts("success")),
                        self._texts("success"))

    def test_generate_refusal_reason_visible(self):
        self.at.button(key="ipd_generate").click().run()  # no clinician ID -> refused
        self.assertTrue(any(t.startswith("Progress note not generated") for t in self._texts("error")),
                        self._texts("error"))

    def test_approval_refusal_reason_visible(self):
        self.at.text_input(key="ipd_gen_by").input("u_dr_demo")
        self.at.button(key="ipd_generate").click().run()
        self.at.button(key="ipd_approve").click().run()  # no approver ID -> refused
        self.assertTrue(any(t.startswith("Approval refused") for t in self._texts("error")), self._texts("error"))


class TestHeartRateEvent(unittest.TestCase):
    def test_heart_rate_becomes_pulse_event(self):
        from stt_extract import run_text_extract
        from ipd.event_extractor import extract_events
        from ipd.models import EventCategory, Role, SourceType
        tj, ej = run_text_extract(NOTE, [{"id": 0, "text": NOTE, "start": 0.0, "end": 0.0}], "c",
                                  use_llm=False, source="image")
        evs = extract_events(ej, encounter_id="e", source_capture_id="c",
                             occurred_at=datetime(2026, 10, 16, 9, tzinfo=timezone.utc), author_id="u",
                             author_role=Role.CONSULTANT, source_type=SourceType.TYPED, source_text=NOTE)
        pulse = [e for e in evs if e.category == EventCategory.VITAL and e.subtype == "pulse"]
        self.assertEqual([e.payload["value"] for e in pulse], [88])


if __name__ == "__main__":
    unittest.main()
