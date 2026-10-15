"""Regression: admitting an MRN that is already admitted must not look like a new admission.

Run:  python -m pytest demo/tests/test_ipd_admission_mismatch.py -q

Reported: "Demo Patient 002", DEMO-0002, Ward 4 / bed 12 showed Ward 5 / bed 11.
Cause: DEMO-0002 already had an active encounter (Ward 5 / bed 11); the
duplicate branch returned SUCCESS and silently ignored the typed name/ward/bed.
Now different details are refused with a clear error, nothing is written and
the existing record is not switched in as if it were the new admission.
Identical resubmission still opens the existing encounter (no duplicate).
Synthetic patients only; temp/in-memory databases, never the local demo DB.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO = os.path.dirname(_HERE)
sys.path.insert(0, _DEMO)

from streamlit.testing.v1 import AppTest  # noqa: E402

from ipd import ui  # noqa: E402
from ipd.models import Role  # noqa: E402
from ipd.store import IpdStore  # noqa: E402


def _admit(store, name, mrn, ward, bed):
    return ui.admit_demo_patient(store, name=name, mrn=mrn, dob=None, sex="", ward=ward, bed=bed,
                                 admitted_by="u_dr_demo", admitted_role=Role.CONSULTANT)


def _counts(store):
    with store.transaction():
        pass
    return {t: store._conn.execute(f"select count(*) from {t}").fetchone()[0]
            for t in ("patients", "encounters", "bed_assignments", "events", "audit_log")}


class TestAdmitExistingMrn(unittest.TestCase):
    def setUp(self):
        self.store = IpdStore()  # in-memory
        o = _admit(self.store, "Demo Patient 0002", "DEMO-0002", "Ward 5", "11")
        self.assertTrue(o.ok and not o.data["duplicate"])
        self.enc = o.data["encounter_id"]

    def tearDown(self):
        self.store.close()

    def test_different_ward_bed_name_refused_and_nothing_written(self):
        before = _counts(self.store)
        o = _admit(self.store, "Demo Patient 002", "DEMO-0002", "Ward 4", "12")
        self.assertFalse(o.ok)  # was ok=True "already admitted" (green) -> looked like Ward 5/11 admission
        msg = o.message
        for needle in ("DEMO-0002", "already admitted", "Ward 5", "11", "Ward 4", "12", "not"):
            self.assertIn(needle, msg)
        self.assertEqual(_counts(self.store), before)
        ctx = ui.find_patient_context(self.store, "DEMO-0002")
        self.assertEqual((ctx["bed"].ward, ctx["bed"].bed, ctx["patient"].name),
                         ("Ward 5", "11", "Demo Patient 0002"))  # existing record untouched

    def test_identical_resubmission_still_opens_existing(self):
        before = _counts(self.store)
        o = _admit(self.store, "Demo Patient 0002", "DEMO-0002", " Ward 5 ", "11")
        self.assertTrue(o.ok and o.data["duplicate"])
        self.assertEqual(o.data["encounter_id"], self.enc)
        self.assertEqual(_counts(self.store), before)

    def test_new_mrn_admits_with_typed_location(self):
        o = _admit(self.store, "Demo Patient 0003", "DEMO-0003", "Ward 4", "12")
        self.assertTrue(o.ok and not o.data["duplicate"])
        ctx = ui.find_patient_context(self.store, "DEMO-0003")
        self.assertEqual((ctx["bed"].ward, ctx["bed"].bed), ("Ward 4", "12"))
        ctx2 = ui.find_patient_context(self.store, "DEMO-0002")
        self.assertEqual((ctx2["bed"].ward, ctx2["bed"].bed), ("Ward 5", "11"))  # no cross-talk


class TestAdmitExistingMrnUi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="medibytes_ipd_mismatch_")
        self.db = os.path.join(self.tmp, "ipd.sqlite")
        env = mock.patch.dict(os.environ, {"MEDIBYTES_IPD_DB": self.db})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        with IpdStore(self.db) as s:
            self.assertTrue(_admit(s, "Demo Patient 0002", "DEMO-0002", "Ward 5", "11").ok)
        self.at = AppTest.from_file(os.path.join(_DEMO, "ipd_app.py"), default_timeout=120)

    def _submit(self, name, mrn, ward, bed):
        at = self.at
        for k, v in (("ipd_adm_name", name), ("ipd_adm_mrn", mrn), ("ipd_adm_ward", ward),
                     ("ipd_adm_bed", bed), ("ipd_adm_by", "u_dr_demo")):
            at.text_input(key=k).input(v)
        at.selectbox(key="ipd_adm_role").select("CONSULTANT")
        at.button(key="ipd_admit").click().run()
        self.assertEqual([str(e.value)[:200] for e in at.exception], [])

    def test_ui_shows_error_and_does_not_present_existing_bed_as_new(self):
        self.at.run()
        self._submit("Demo Patient 002", "DEMO-0002", "Ward 4", "12")
        errors = " ".join(str(e.value) for e in self.at.error)
        self.assertIn("already admitted", errors)
        self.assertIn("Ward 5", errors)
        self.assertFalse(any("already admitted" in str(s.value) for s in self.at.success))
        # the existing encounter is not opened as if it were the requested admission
        self.assertFalse(any("Ward 5 / bed 11" in str(i.value) for i in self.at.info))
        with IpdStore(self.db) as s:
            self.assertEqual(len(s.list_encounters(ui.find_patient_context(s, "DEMO-0002")["patient"].id)), 1)

    def test_ui_new_mrn_shows_typed_location(self):
        self.at.run()
        self._submit("Demo Patient 0003", "DEMO-0003", "Ward 4", "12")
        self.assertTrue(any("Ward 4 / bed 12" in str(i.value) for i in self.at.info))
        self.assertTrue(any("Patient admitted" in str(s.value) for s in self.at.success))


if __name__ == "__main__":
    unittest.main()
