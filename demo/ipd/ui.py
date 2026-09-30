"""IPD P1 step 9: Streamlit UI for the typed-text ward-round vertical slice.

Two layers:
  1. Orchestration helpers (no Streamlit): thin calls into the existing IPD
     services — encounter.admit_patient, capture.capture_typed_ward_round,
     Timeline, progress_note.generate_progress_note, documents.*, render.* —
     returning plain dicts for display. No clinical rule is re-implemented.
  2. render_app(st, db_path): the page. Every clinical write happens only
     inside an explicit button click; reruns only read.

Persistence: one SQLite file OUTSIDE the repository (default under the OS
app-data / temp directory, override with MEDIBYTES_IPD_DB). A new IpdStore is
opened per rerun (sqlite connections must not cross Streamlit threads).
Session state holds UI selections only (selected MRN, form defaults).
"""
import hashlib
import os
import re
import tempfile
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import List, Optional, Union

from .capture import WARD_ROUND_ROLES, CaptureError, CaptureProcessingError, capture_typed_ward_round
from .documents import (
    APPROVER_ROLES, NOTE_CATEGORIES, ApprovalError, DocumentError, approve, check_approval,
    create_progress_note_document, regenerate_progress_note, staleness,
)
from .encounter import ADMITTING_ROLES, admit_patient
from .models import IST, DocumentType, EncounterStatus, EventCategory, Patient, Role, now
from .progress_note import ProgressNoteError, generate_progress_note
from .render import DOCX_AVAILABLE, render_progress_note_docx, render_progress_note_html
from .store import IpdStore
from .timeline import ACTIVE_ENCOUNTER_STATUSES, Timeline, TimelineError

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEMO_MRN_PREFIX = "DEMO-"
CLINICIAN_ROLES = (Role.CONSULTANT, Role.RESIDENT, Role.INTENSIVIST, Role.WARD_NURSE, Role.ICU_NURSE,
                   Role.PHARMACIST, Role.ADMIN)


class UiError(ValueError):
    """Clinician-facing message (safe to show)."""


@dataclass
class Outcome:
    ok: bool
    message: str
    details: List[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)
    debug: str = ""


# ================================================================ database location
def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(root)]) == os.path.realpath(root)
    except ValueError:  # different drives on Windows
        return False


def default_db_path() -> str:
    """MEDIBYTES_IPD_DB, else <app-data or temp>/MediBytes/ipd_demo.sqlite. Never inside the repo."""
    path = os.environ.get("MEDIBYTES_IPD_DB")
    if not path:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME") or tempfile.gettempdir()
        path = os.path.join(base, "MediBytes", "ipd_demo.sqlite")
    path = os.path.abspath(path)
    if _inside(path, REPO_ROOT):
        raise UiError(f"refusing to place the demo database inside the repository: {path}")
    return path


def open_store(path: str) -> IpdStore:
    if path != ":memory:":
        if _inside(path, REPO_ROOT):
            raise UiError("refusing to open a database inside the repository")
        os.makedirs(os.path.dirname(path), exist_ok=True)
    return IpdStore(path)


# ================================================================ helpers (no Streamlit)
def _fmt(ts: Optional[datetime]) -> str:
    return ts.astimezone(IST).strftime("%Y-%m-%d %H:%M") if ts else ""


def _safe(exc: Exception) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def find_patient_context(store: IpdStore, mrn: str) -> Optional[dict]:
    """Patient + latest encounter + current bed for an MRN (read-only)."""
    p = store.get_patient_by_mrn((mrn or "").strip())
    if p is None:
        return None
    encs = store.list_encounters(p.id)
    enc = encs[-1] if encs else None
    bed = store.get_active_bed(enc.id) if enc else None
    return {"patient": p, "encounter": enc, "bed": bed,
            "active": bool(enc and enc.status in ACTIVE_ENCOUNTER_STATUSES)}


def admit_demo_patient(store: IpdStore, *, name: str, mrn: str, dob: Optional[date], sex: str, ward: str, bed: str,
                       admitted_by: str, admitted_role: Role, admit_at: Optional[datetime] = None) -> Outcome:
    """Explicit admission via encounter.admit_patient. An MRN with an active encounter is not re-admitted."""
    mrn = (mrn or "").strip()
    if not mrn.upper().startswith(DEMO_MRN_PREFIX):
        return Outcome(False, f"Demo MRNs must start with {DEMO_MRN_PREFIX} (synthetic data only).")
    if not (name or "").strip():
        return Outcome(False, "Patient name is required.")
    existing = find_patient_context(store, mrn)
    if existing and existing["active"]:
        return Outcome(True, "Patient already admitted — showing the existing active encounter (no new admission).",
                       data={"encounter_id": existing["encounter"].id, "duplicate": True})
    patient = existing["patient"] if existing else Patient(mrn=mrn, name=name.strip(), dob=dob, sex=(sex or "").strip())
    # The UI works at minute precision (capture time is entered to the minute), so admission is too;
    # otherwise a round captured in the admission minute would fall before the note window.
    admit_at = admit_at or now().replace(second=0, microsecond=0)
    try:
        r = admit_patient(store, patient, ward=ward, bed=bed, admitted_by=admitted_by, admitted_role=admitted_role,
                          admit_at=admit_at)
    except (ValueError, KeyError) as e:
        return Outcome(False, f"Admission refused: {e}", debug=_safe(e))
    return Outcome(True, "Patient admitted.", data={"encounter_id": r.encounter.id, "duplicate": False})


def capture_key(encounter_id: str, text: str, captured_at: datetime, author_id: str) -> str:
    """Deterministic capture id: resubmitting the identical note reprocesses it instead of duplicating."""
    raw = "|".join([encounter_id, text, captured_at.isoformat(), author_id.strip()])
    return "cap_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def event_summary(e) -> str:
    """Display text for an event, taken verbatim from its payload."""
    p = e.payload
    if e.retraction:
        return f"retraction — {e.reason}"
    if e.category == EventCategory.VITAL:
        if e.subtype == "bp":
            val = f"{p.get('systolic')}/{p.get('diastolic')}"
        else:
            val = f"{p.get('value')}"
        return val + (f" {p['unit']}" if p.get("unit") else "")
    if e.category == EventCategory.MEDICATION_ORDER:
        parts = [str(p.get(k)) for k in ("name", "dose", "unit", "frequency", "duration") if p.get(k) not in (None, "")]
        return "PROPOSED (not prescribed): " + " ".join(parts)
    for k in ("term", "substance", "text", "ward"):
        if p.get(k):
            return str(p[k]) + (f" / bed {p['bed']}" if k == "ward" and p.get("bed") else "")
    return ""


def process_ward_round(store: IpdStore, *, encounter_id: str, text: str, author_id: str, author_role: Role,
                       captured_at: datetime) -> Outcome:
    """Explicit call to capture.capture_typed_ward_round (location taken from the encounter's bed)."""
    bed = store.get_active_bed(encounter_id)
    location = {"ward": bed.ward, "bed": bed.bed, "unit_type": bed.unit_type.value} if bed else {}
    enc = store.get_encounter(encounter_id)
    if enc is not None and captured_at < enc.admit_at:
        return Outcome(False, f"Ward round not recorded: capture time is before admission ({_fmt(enc.admit_at)}).")
    try:
        r = capture_typed_ward_round(store, encounter_id=encounter_id, text=text, author_id=author_id,
                                     author_role=author_role, location=location, captured_at=captured_at,
                                     capture_id=capture_key(encounter_id, text or "", captured_at, author_id or ""))
    except CaptureError as e:
        return Outcome(False, f"Ward round not recorded: {e}")
    except CaptureProcessingError as e:
        return Outcome(False, "Ward round could not be processed. The note was saved as FAILED and no clinical "
                              "events were added.", debug=f"{e}\n\n{_safe(e)}",
                       data={"capture_id": e.result.capture.id, "status": e.result.status.value})
    items = [{"category": ev.category.value, "type": ev.subtype, "value": event_summary(ev),
              "verification": ev.verification.value} for ev in r.events]
    return Outcome(True, "Ward round processed.", data={
        "capture_id": r.capture.id, "status": r.status.value, "items": items,
        "n_events": len(r.events), "n_appended": len(r.appended_event_ids),
        "n_duplicates": len(r.duplicate_event_ids), "skipped": [dict(s) for s in r.skipped],
        "has_medication": any(ev.category == EventCategory.MEDICATION_ORDER for ev in r.events),
        "pipeline_info": dict(r.capture.pipeline_info)})


def timeline_view(store: IpdStore, encounter_id: str, full_history: bool) -> dict:
    """Rows for display + conflicts (read-only)."""
    tl = Timeline(store)
    events = tl.history(encounter_id, active_only=not full_history)
    rows = [{"time": _fmt(e.occurred_at), "category": e.category.value, "type": e.subtype,
             "value": event_summary(e), "verification": e.verification.value, "source": e.source_type.value,
             "status": tl.status_of(e.id), "author": f"{e.author_id} ({e.author_role.value})",
             "reason": e.reason, "event_id": e.id} for e in events]
    conflicts = [{"category": c.category.value, "key": c.key, "time": _fmt(c.occurred_from),
                  "values": list(c.values), "event_ids": list(c.event_ids)} for c in tl.detect_conflicts(encounter_id)]
    return {"rows": rows, "conflicts": conflicts}


_DECIMAL_INPUT = re.compile(r"^\d{1,4}(?:\.\d{1,3})?$")


def parse_decimal_input(raw) -> Union[int, float]:
    """Entered clinical number -> the same number, without float drift.

    "100.4" / "100.40" -> 100.4 · "101" / "101.0" -> 101. Plain non-negative decimals only
    (no signs, exponents or separators). No arithmetic is applied to the value.
    """
    text = raw if isinstance(raw, str) else ("" if raw is None or isinstance(raw, bool) else str(raw))
    text = text.strip()
    if not _DECIMAL_INPUT.match(text):
        raise UiError(f"enter a plain number such as 100.4 (got {text!r})" if text else "a value is required")
    d = Decimal(text)
    return int(d) if d == d.to_integral_value() else float(format(d.normalize(), "f"))


def correct_vital(store: IpdStore, event_id: str, *, values: dict, unit: str, reason: str, author_id: str,
                  author_role: Role) -> Outcome:
    """Explicit correction via Timeline.correct (vitals only in the P1 UI).

    `values` holds the entered text (or numbers) per field; each is parsed exactly as entered.
    """
    e = store.get_event(event_id)
    if e is None or e.category != EventCategory.VITAL:
        return Outcome(False, "Only vital-sign events can be corrected here.")
    try:
        payload = {k: parse_decimal_input(v) for k, v in values.items()}
    except UiError as err:
        return Outcome(False, f"Correction refused: {err}")
    payload["unit"] = (unit.strip() or None) if isinstance(unit, str) else None
    try:
        new = Timeline(store).correct(event_id, payload=payload, reason=reason,
                                      author_id=(author_id or "").strip(), author_role=author_role)
    except (TimelineError, ValueError) as err:
        return Outcome(False, f"Correction refused: {err}")
    return Outcome(True, f"Correction recorded: {event_summary(new)}. The original remains in history as superseded.",
                   data={"event_id": new.id, "payload": dict(new.payload)})


def retract_event(store: IpdStore, event_id: str, *, reason: str, author_id: str, author_role: Role) -> Outcome:
    try:
        new = Timeline(store).retract(event_id, reason=reason, author_id=author_id, author_role=author_role)
    except (TimelineError, ValueError) as err:
        return Outcome(False, f"Retraction refused: {err}")
    return Outcome(True, "Retraction recorded; the original remains in history.", data={"event_id": new.id})


def current_note(store: IpdStore, encounter_id: str) -> Optional[dict]:
    """The encounter's progress-note document (P1: one per encounter) with its current state (read-only)."""
    docs = store.list_documents(encounter_id, DocumentType.PROGRESS_NOTE)
    if not docs:
        return None
    d = docs[0]
    v = store.get_document_version(d.id, d.current_version)
    rep = staleness(store, d.id, v.version)
    after = [e for e in Timeline(store).active_events(encounter_id, since=d.window_to)
             if e.category in NOTE_CATEGORIES]
    return {"document": d, "version": v, "versions": store.list_document_versions(d.id),
            "stale": rep.stale or v.stale, "stale_reasons": list(rep.reasons),
            "needs_review": bool(v.content.get("needs_review") or v.content.get("conflicts")),
            "approved": v.approved_at is not None, "n_events_after_window": len(after),
            "problems_preview": check_approval(store, d.id, v.version, Role.CONSULTANT)}


def generate_note(store: IpdStore, encounter_id: str, *, generated_by: str, generated_role: Role,
                  generated_at: Optional[datetime] = None) -> Outcome:
    """Explicit: create the encounter's first note; never creates a second independent document."""
    at = generated_at or now()
    if current_note(store, encounter_id) is not None:
        return Outcome(False, "A progress note already exists for this encounter — use Regenerate instead.")
    enc = store.get_encounter(encounter_id)
    try:
        draft = generate_progress_note(Timeline(store), encounter_id, window_from=enc.admit_at,
                                       window_to=at + timedelta(minutes=1), generated_by=generated_by,
                                       generated_at=at)
        doc, v = create_progress_note_document(store, draft, created_by=generated_by, created_role=generated_role)
    except (ProgressNoteError, DocumentError, ValueError) as e:
        return Outcome(False, f"Progress note not generated: {e}")
    return Outcome(True, "Progress note draft created (version 1).", data={"document_id": doc.id, "version": v.version})


def regenerate_note(store: IpdStore, document_id: str, *, generated_by: str, generated_role: Role,
                    extend_window: bool = False, generated_at: Optional[datetime] = None) -> Outcome:
    """Explicit regeneration via documents.regenerate_progress_note (optionally extending window_to to now)."""
    at = generated_at or now()
    try:
        doc, v = regenerate_progress_note(store, document_id, generated_by=generated_by, generated_role=generated_role,
                                          generated_at=at,
                                          window_to=(at + timedelta(minutes=1)) if extend_window else None)
    except (ProgressNoteError, DocumentError, ValueError) as e:
        return Outcome(False, f"Regeneration refused: {e}")
    return Outcome(True, f"Progress note regenerated (version {v.version}).", data={"version": v.version})


def approve_note(store: IpdStore, document_id: str, *, version: int, approver_id: str, approver_role: Role,
                 approved_at: Optional[datetime] = None) -> Outcome:
    """Explicit approval via documents.approve; refusal reasons are passed through unchanged."""
    try:
        r = approve(store, document_id, version=version, approved_by=approver_id, approved_role=approver_role,
                    approved_at=approved_at or now())
    except ApprovalError as e:
        return Outcome(False, "Approval refused.", details=list(e.problems))
    except (DocumentError, ValueError) as e:
        return Outcome(False, f"Approval refused: {e}")
    v = r.version
    msg = "Already approved." if r.already_approved else "Approved."
    return Outcome(True, msg, data={"approved_by": v.approved_by, "approved_role": v.approved_role.value,
                                    "approved_at": _fmt(v.approved_at), "version": v.version})


def export_html(store: IpdStore, document_id: str, version: int) -> bytes:
    return render_progress_note_html(store, document_id, version).encode("utf-8")


def export_docx(store: IpdStore, document_id: str, version: int) -> Optional[bytes]:
    return render_progress_note_docx(store, document_id, version) if DOCX_AVAILABLE else None


# ================================================================ page
_BADGE = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴", "NIL": "⚪"}
# ID fields have no placeholder: a grey sample ID looked like an entered value while the field was empty.
_ID_HELP = "Type your own clinician ID. It is recorded exactly as entered; an empty ID is refused."


def _show(st, o: Outcome):
    (st.success if o.ok else st.error)(o.message)
    for d in o.details:
        st.markdown(f"- {d}")
    if o.debug:
        with st.expander("Technical details (developer)"):
            st.code(o.debug)


def _act(st, ss, slot: str, o: Outcome) -> None:
    """Remember the outcome of an explicit action; the page reruns once it has fully rendered.

    Rerunning mid-page would skip rendering the later tabs, and Streamlit drops the values of
    widgets that were not rendered (e.g. clinician IDs typed in another tab).
    """
    ss[f"ipd_flash_{slot}"] = o
    ss["ipd_needs_rerun"] = True


def _flash(st, ss, slot: str) -> None:
    o = ss.pop(f"ipd_flash_{slot}", None)
    if o is not None:
        _show(st, o)


def _now_parts():
    t = now()
    return t.date(), time(t.hour, t.minute)


def render_app(st, db_path: Optional[str] = None) -> None:
    path = db_path or default_db_path()
    ss = st.session_state
    ss.setdefault("ipd_mrn", "")
    store = open_store(path)
    try:
        _page(st, store, ss)
        with st.expander("Developer / debug"):
            st.caption(f"Demo database: {path}")
            st.caption("Extraction: existing run_text_extract, deterministic mode, no LLM. "
                       "Clinical writes happen only on explicit button clicks.")
    finally:
        store.close()
    if ss.pop("ipd_needs_rerun", False):  # after an explicit write: refresh every tab once
        st.rerun()


def _page(st, store: IpdStore, ss) -> None:
    st.title("MediBytes IPD — Ward Round → Progress Note (P1 demo)")
    st.warning("DEMO / SYNTHETIC DATA ONLY — not for real patients. Drafts are not approved for clinical use "
               "until a clinician approves them.")
    tabs = st.tabs(["A · Patient / Admission", "B · Ward Round", "C · Timeline", "D · Progress Note",
                    "E · Review / Approval", "F · Export"])

    # ---------------- A. admission
    with tabs[0]:
        st.subheader("Open an admitted patient")
        c1, c2 = st.columns([3, 1])
        mrn_in = c1.text_input("MRN", value=ss["ipd_mrn"], key="ipd_open_mrn", placeholder="DEMO-0001")
        if c2.button("Open", key="ipd_open"):
            ss["ipd_mrn"] = mrn_in.strip()
        st.subheader("Admit a synthetic patient")
        a1, a2 = st.columns(2)
        name = a1.text_input("Patient name", key="ipd_adm_name", placeholder="Demo Patient 0001")
        mrn = a2.text_input("MRN (must start with DEMO-)", key="ipd_adm_mrn", placeholder="DEMO-0001")
        dob = a1.date_input("Date of birth", value=None, key="ipd_adm_dob", min_value=date(1900, 1, 1))
        sex = a2.selectbox("Sex", ["", "F", "M", "Other"], key="ipd_adm_sex")
        ward = a1.text_input("Ward", key="ipd_adm_ward", placeholder="Ward 4")
        bed = a2.text_input("Bed", key="ipd_adm_bed", placeholder="12")
        by = a1.text_input("Admitting clinician ID (required)", key="ipd_adm_by", help=_ID_HELP)
        role = a2.selectbox("Admitting clinician role", sorted(r.value for r in ADMITTING_ROLES), key="ipd_adm_role")
        if st.button("Admit patient", type="primary", key="ipd_admit"):
            o = admit_demo_patient(store, name=name, mrn=mrn, dob=dob, sex=sex, ward=ward, bed=bed,
                                   admitted_by=by, admitted_role=Role(role))
            if o.ok:
                ss["ipd_mrn"] = mrn.strip()
            _act(st, ss, "admit", o)
        _flash(st, ss, "admit")

    ctx = find_patient_context(store, ss["ipd_mrn"]) if ss["ipd_mrn"] else None
    with tabs[0]:
        if ctx:
            p, enc, bedr = ctx["patient"], ctx["encounter"], ctx["bed"]
            st.info(f"**{p.name}** · MRN {p.mrn} · encounter `{enc.id if enc else '—'}` · status "
                    f"{enc.status.value if enc else '—'} · {bedr.ward + ' / bed ' + bedr.bed if bedr else 'no bed'}")
        elif ss["ipd_mrn"]:
            st.error(f"No patient with MRN {ss['ipd_mrn']}.")
    if not ctx or not ctx["encounter"]:
        for t in tabs[1:]:
            with t:
                st.info("Admit or open a patient first (tab A).")
        return
    enc, bedr = ctx["encounter"], ctx["bed"]

    # ---------------- B. ward round
    with tabs[1]:
        if not ctx["active"]:
            st.warning(f"Encounter is {enc.status.value}; no new ward rounds can be recorded.")
        st.caption(f"Location: {bedr.ward} / bed {bedr.bed} ({bedr.unit_type.value})" if bedr else "Location: none")
        text = st.text_area("Typed Ward-Round Note", key="ipd_round_text", height=160,
                            placeholder="Patient has fever. No chest pain.\nBP 118/76, pulse 82.\n"
                                        "Temperature: 101 F\nDiagnosis: viral fever.\nStart paracetamol 650 mg TDS.")
        b1, b2 = st.columns(2)
        rby = b1.text_input("Clinician ID (required)", key="ipd_round_by", help=_ID_HELP)
        rrole = b2.selectbox("Clinician role", sorted(r.value for r in WARD_ROUND_ROLES), key="ipd_round_role")
        d0, t0 = _now_parts()
        ss.setdefault("ipd_round_date", d0)
        ss.setdefault("ipd_round_time", t0)
        cdate = b1.date_input("Capture date", key="ipd_round_date")
        ctime = b2.time_input("Capture time (IST)", key="ipd_round_time", step=60)
        st.caption("Medication mentioned in the note is recorded as PROPOSED / UNVERIFIED — never as a prescription.")
        if st.button("Process Ward Round", type="primary", key="ipd_process", disabled=not ctx["active"]):
            at = datetime.combine(cdate, ctime, tzinfo=IST)
            o = process_ward_round(store, encounter_id=enc.id, text=text, author_id=rby, author_role=Role(rrole),
                                   captured_at=at)
            ss["ipd_last_capture"] = o
            ss["ipd_needs_rerun"] = True
        o = ss.get("ipd_last_capture")
        if o is not None:
            _show(st, o)
            if o.ok:
                d = o.data
                st.write(f"Events generated: **{d['n_events']}** · newly added: **{d['n_appended']}** · "
                         f"already on timeline (duplicates): **{d['n_duplicates']}**")
                if d["items"]:
                    st.dataframe(d["items"], use_container_width=True, hide_index=True)
                if d["has_medication"]:
                    st.warning("Medication items are PROPOSED and UNVERIFIED — not prescriptions.")
                if d["skipped"]:
                    with st.expander(f"Skipped extractor items ({len(d['skipped'])})"):
                        st.dataframe(d["skipped"], use_container_width=True, hide_index=True)
                with st.expander("Technical details (developer)"):
                    st.json(d["pipeline_info"])

    # ---------------- C. timeline
    with tabs[2]:
        full = st.toggle("Show full history", key="ipd_full_history",
                         help="Off: active events only. On: include superseded/retracted history.")
        tv = timeline_view(store, enc.id, full)
        if tv["conflicts"]:
            st.error(f"⚠ {len(tv['conflicts'])} conflict(s): different values recorded at the same time. "
                     "They are NOT resolved automatically — review and correct/retract as appropriate.")
            st.dataframe([{k: c[k] for k in ("category", "key", "time", "values")} for c in tv["conflicts"]],
                         use_container_width=True, hide_index=True)
        st.dataframe([{k: r[k] for k in ("time", "category", "type", "value", "verification", "source", "status")}
                      for r in tv["rows"]], use_container_width=True, hide_index=True)
        with st.expander("Correct or retract an event (creates a new event; nothing is overwritten)"):
            _flash(st, ss, "fix")
            active = {r["event_id"]: r for r in timeline_view(store, enc.id, False)["rows"] if r["category"] != "ADT"}
            if not active:
                st.caption("No active clinical events.")
            else:
                # options are stable event ids; labels are display only
                eid = st.selectbox("Event", list(active), key="ipd_fix_event",
                                   format_func=lambda i: f"{active[i]['time']} · {active[i]['category']}/"
                                                         f"{active[i]['type']} · {active[i]['value']}")
                pick = active[eid]
                n = ss.setdefault("ipd_fix_nonce", 0)  # bumped only after a successful save -> fresh empty form
                with st.form(f"ipd_fix_form_{n}", clear_on_submit=False):
                    reason = st.text_input("Reason (required)", key=f"ipd_fix_reason_{n}")
                    fby = st.text_input("Clinician ID (required)", key=f"ipd_fix_by_{n}")
                    frole = st.selectbox("Clinician role", sorted(r.value for r in WARD_ROUND_ROLES),
                                         key=f"ipd_fix_role_{n}")
                    is_vital = pick["category"] == "VITAL"
                    if is_vital:
                        if pick["type"] == "bp":
                            c1, c2 = st.columns(2)
                            vals = {"systolic": c1.text_input("Corrected systolic", key=f"ipd_fix_sys_{n}"),
                                    "diastolic": c2.text_input("Corrected diastolic", key=f"ipd_fix_dia_{n}")}
                        else:
                            vals = {"value": st.text_input("Corrected value (e.g. 100.4)", key=f"ipd_fix_val_{n}")}
                        unit = st.text_input("Unit (leave blank if not stated)", key=f"ipd_fix_unit_{n}")
                    else:
                        st.caption("Only vital signs can be corrected here; other events can be retracted.")
                    do_fix = st.form_submit_button("Record correction", disabled=not is_vital)
                    do_retract = st.form_submit_button("Retract event")
                if do_fix and is_vital:
                    o = correct_vital(store, eid, values=vals, unit=unit, reason=reason, author_id=fby,
                                      author_role=Role(frole))
                    if o.ok:
                        ss["ipd_fix_nonce"] = n + 1  # reset the form only after persistence succeeded
                    _act(st, ss, "fix", o)
                elif do_retract:
                    o = retract_event(store, eid, reason=reason, author_id=fby, author_role=Role(frole))
                    if o.ok:
                        ss["ipd_fix_nonce"] = n + 1
                    _act(st, ss, "fix", o)

    # ---------------- D. progress note
    note = current_note(store, enc.id)
    with tabs[3]:
        g1, g2 = st.columns(2)
        gby = g1.text_input("Clinician ID (required)", key="ipd_gen_by", help=_ID_HELP)
        grole = g2.selectbox("Clinician role", sorted(r.value for r in APPROVER_ROLES), key="ipd_gen_role")
        if note is None:
            if st.button("Generate Progress Note", type="primary", key="ipd_generate"):
                _act(st, ss, "note", generate_note(store, enc.id, generated_by=gby, generated_role=Role(grole)))
        else:
            if note["stale"]:
                st.error("Progress Note is stale — regeneration required.")
                for r in note["stale_reasons"]:
                    st.caption(f"• {r}")
            if note["n_events_after_window"]:
                st.info(f"{note['n_events_after_window']} clinical event(s) recorded after this note's window.")
            r1, r2 = st.columns(2)
            if r1.button("Regenerate Progress Note", key="ipd_regen", disabled=not note["stale"]):
                _act(st, ss, "note", regenerate_note(store, note["document"].id, generated_by=gby,
                                                     generated_role=Role(grole)))
            if r2.button("Regenerate and extend window to now", key="ipd_regen_extend",
                         disabled=not (note["stale"] or note["n_events_after_window"])):
                _act(st, ss, "note", regenerate_note(store, note["document"].id, generated_by=gby,
                                                     generated_role=Role(grole), extend_window=True))
        _flash(st, ss, "note")
        if note is not None:
            _note_view(st, note)

    # ---------------- E. approval
    with tabs[4]:
        if note is None:
            st.info("Generate a progress note first (tab D).")
        else:
            v = note["version"]
            st.write(f"Current version **{v.version}** ({v.change_type.value})")
            if note["approved"]:
                st.success(f"APPROVED — by {v.approved_by} ({v.approved_role.value}) at {_fmt(v.approved_at)} IST")
                st.caption("Approval does not change event verification: YELLOW items remain unverified on the timeline.")
            else:
                st.error("DRAFT — NOT APPROVED FOR CLINICAL USE")
                if note["problems_preview"]:
                    st.caption("Approval will currently be refused for:")
                    for pr in note["problems_preview"]:
                        st.caption(f"• {pr}")
                e1, e2 = st.columns(2)
                aby = e1.text_input("Approver ID (required)", key="ipd_appr_by", help=_ID_HELP)
                arole = e2.selectbox("Approver role", [r.value for r in CLINICIAN_ROLES], key="ipd_appr_role")
                if st.button("Approve Current Version", type="primary", key="ipd_approve"):
                    _act(st, ss, "approve", approve_note(store, note["document"].id, version=v.version,
                                                         approver_id=aby, approver_role=Role(arole)))
            _flash(st, ss, "approve")

    # ---------------- F. export
    with tabs[5]:
        if note is None:
            st.info("Generate a progress note first (tab D).")
        else:
            nums = [x.version for x in note["versions"]]
            sel = st.selectbox("Version to export", nums, index=len(nums) - 1, key="ipd_export_version")
            sv = next(x for x in note["versions"] if x.version == sel)
            st.caption("APPROVED" if sv.approved_at else "DRAFT — NOT APPROVED FOR CLINICAL USE")
            did = note["document"].id
            st.download_button("Download HTML", export_html(store, did, sel), file_name=f"progress_note_v{sel}.html",
                               mime="text/html", key="ipd_dl_html")
            data = export_docx(store, did, sel)
            if data is not None:
                st.download_button("Download DOCX", data, file_name=f"progress_note_v{sel}.docx",
                                   mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                   key="ipd_dl_docx")
            else:
                st.caption("DOCX export unavailable (python-docx not installed).")


def _note_view(st, note: dict) -> None:
    v = note["version"]
    flags = ["APPROVED" if note["approved"] else "DRAFT"]
    if note["stale"]:
        flags.append("STALE")
    if note["needs_review"]:
        flags.append("REVIEW REQUIRED")
    st.markdown(f"**Progress Note — version {v.version}** · " + " · ".join(f"`{f}`" for f in flags))
    if note["needs_review"]:
        st.warning("CLINICIAN REVIEW REQUIRED — conflicting values are shown and have not been resolved.")
    for s in v.content.get("sections", []):
        st.markdown(f"**{s['key']} — {s['title']}**" + (" _(required)_" if s.get("required") else ""))
        for l in s["lines"]:
            flag = " ⚠ review required" if l.get("needs_review") else ""
            text = f"_{l['text']}_" if l.get("kind") == "placeholder" else l["text"]
            st.markdown(f"{_BADGE.get(l['color'], '')} `{l['color']}` {text}{flag}")
