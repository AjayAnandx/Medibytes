"""IPD P1 step 6: deterministic SOAP progress-note DRAFT from the Patient Event Timeline.

Read-only: it queries the timeline (active events in [window_from, window_to))
and the conflict detector, and returns a ProgressNoteDraft. It never writes,
verifies, corrects or resolves anything, and it has no LLM.

Sections
  S  SYMPTOM              "<Term> present." / "<Term> denied."
  O  VITAL                "<time> BP 118/76", "<time> Temperature 101 F" (unit only if on the event)
  A  DIAGNOSIS            "Provisional diagnosis: <text>."
  P  PLAN + MEDICATION_ORDER/proposed
                          "Follow-up: <text>."
                          "Proposed medication (not confirmed, not prescribed): <drug dose unit ...>."

Every clinical line carries the event id(s) it came from. Placeholders
(S/A empty -> RED, O/P empty -> NIL) are marked kind="placeholder" and carry
no event ids. Events that are in a detected conflict keep all their values;
their lines and section are flagged needs_review. Nothing is chosen.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .models import DocumentStatus, Event, EventCategory, IST, Verification, require_aware
from .timeline import Conflict, Timeline

GENERATOR = "ipd.progress_note/p1-v1"

CLINICAL, PLACEHOLDER = "clinical", "placeholder"
GREEN, YELLOW, RED, NIL = "GREEN", "YELLOW", "RED", "NIL"

SECTIONS = (("S", "Subjective", True), ("O", "Objective", False), ("A", "Assessment", True), ("P", "Plan", False))
PLACEHOLDERS = {"S": (RED, "No subjective information captured."),
                "O": (NIL, "No objective findings captured."),
                "A": (RED, "No assessment information captured."),
                "P": (NIL, "No plan captured.")}
_VITAL_LABELS = {"bp": "BP", "temperature": "Temperature", "spo2": "SpO2", "pulse": "Pulse", "rr": "RR",
                 "map": "MAP", "gcs": "GCS", "pain_score": "Pain score"}
_DX_PREFIX = {"provisional": "Provisional diagnosis", "confirmed": "Diagnosis", "ruled_out": "Ruled out"}
REVIEW_CONFLICT = "conflicting values recorded at the same time; clinician review required"


class ProgressNoteError(ValueError):
    pass


@dataclass(frozen=True)
class NoteLine:
    text: str
    event_ids: Tuple[str, ...]
    kind: str = CLINICAL          # "clinical" (supported by events) | "placeholder" (not a fact)
    color: str = YELLOW           # GREEN verified · YELLOW unverified · RED required-missing · NIL optional-empty
    needs_review: bool = False
    review_reason: str = ""

    def to_dict(self) -> dict:
        return {"text": self.text, "event_ids": list(self.event_ids), "kind": self.kind, "color": self.color,
                "needs_review": self.needs_review, "review_reason": self.review_reason}


@dataclass(frozen=True)
class NoteSection:
    key: str
    title: str
    required: bool
    lines: Tuple[NoteLine, ...]

    @property
    def needs_review(self) -> bool:
        return any(l.needs_review for l in self.lines)

    @property
    def has_red(self) -> bool:
        return any(l.color == RED for l in self.lines)

    def to_dict(self) -> dict:
        return {"key": self.key, "title": self.title, "required": self.required, "needs_review": self.needs_review,
                "lines": [l.to_dict() for l in self.lines]}


@dataclass(frozen=True)
class ProgressNoteDraft:
    encounter_id: str
    window_from: datetime
    window_to: datetime
    generated_at: datetime
    generated_by: str
    sections: Tuple[NoteSection, ...]
    source_event_ids: Tuple[str, ...]
    conflicts: Tuple[Conflict, ...] = ()
    omitted: Tuple[dict, ...] = ()        # events in scope that could not be rendered, with reason
    status: DocumentStatus = DocumentStatus.DRAFT
    generator: str = GENERATOR

    def section(self, key: str) -> NoteSection:
        return next(s for s in self.sections if s.key == key)

    @property
    def needs_review(self) -> bool:
        return any(s.needs_review for s in self.sections)

    @property
    def red_sections(self) -> Tuple[str, ...]:
        return tuple(s.key for s in self.sections if s.has_red)

    def to_content(self) -> dict:
        """JSON-serialisable content (for a later DocumentVersion)."""
        return {"type": "progress_note", "generator": self.generator, "status": self.status.value,
                "encounter_id": self.encounter_id, "window_from": self.window_from.isoformat(),
                "window_to": self.window_to.isoformat(), "generated_at": self.generated_at.isoformat(),
                "generated_by": self.generated_by, "sections": [s.to_dict() for s in self.sections],
                "source_event_ids": list(self.source_event_ids), "needs_review": self.needs_review,
                "conflicts": [{"category": c.category.value, "key": c.key, "event_ids": list(c.event_ids),
                               "values": list(c.values)} for c in self.conflicts],
                "omitted": [dict(o) for o in self.omitted]}


# ---------------------------------------------------------------- formatting (no inference)
def _num(v) -> str:
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _sentence(s: str) -> str:
    s = s.strip()
    return s if s.endswith((".", "!", "?")) else s + "."


def _color(e: Event) -> str:
    return GREEN if e.verification == Verification.VERIFIED else YELLOW


def _symptom(e: Event) -> Optional[str]:
    term = str(e.payload.get("term") or e.payload.get("text") or "").strip()
    if not term or e.subtype not in ("present", "denied"):
        return None
    return f"{_cap(term)} {e.subtype}."


def _vital(e: Event, stamp: str) -> Optional[str]:
    p, label = e.payload, _VITAL_LABELS.get(e.subtype, _cap(e.subtype.replace("_", " ")))
    unit = p.get("unit")
    unit = f" {unit}" if isinstance(unit, str) and unit.strip() else ""
    if e.subtype == "bp":
        if p.get("systolic") is None or p.get("diastolic") is None:
            return None
        value = f"{_num(p['systolic'])}/{_num(p['diastolic'])}"
    else:
        if p.get("value") is None:
            return None
        value = _num(p["value"])
    return f"{stamp}{label} {value}{unit}"


def _diagnosis(e: Event) -> Optional[str]:
    text = str(e.payload.get("text") or "").strip()
    prefix = _DX_PREFIX.get(e.subtype)
    if not text or prefix is None:
        return None
    code = str(e.payload.get("icd10") or "").strip()
    return _sentence(f"{prefix}: {text}" + (f" (ICD-10 {code})" if code else ""))


def _plan(e: Event) -> Optional[str]:
    text = str(e.payload.get("text") or "").strip()
    if not text:
        return None
    return _sentence(("Follow-up: " if e.subtype == "followup" else "Plan: ") + text)


def _medication(e: Event) -> Optional[str]:
    if e.subtype != "proposed":
        return None  # only proposed orders exist in P1; never render as active
    p = e.payload
    name = str(p.get("name") or "").strip()
    if not name:
        return None
    parts = [name]
    if p.get("dose") is not None:
        parts.append(_num(p["dose"]) + (f" {p['unit']}" if p.get("unit") else ""))
    elif p.get("unit"):
        parts.append(str(p["unit"]))
    for k in ("frequency", "duration"):
        if p.get(k):
            parts.append(str(p[k]))
    missing = [m for m in (p.get("missing") or []) if isinstance(m, str)]
    tail = f" (not stated: {', '.join(missing)})" if missing else ""
    return f"Proposed medication (not confirmed, not prescribed): {' '.join(parts)}{tail}."


# ---------------------------------------------------------------- generator
def generate_progress_note(timeline: Timeline, encounter_id: str, *, window_from: datetime, window_to: datetime,
                           generated_by: str, generated_at: datetime) -> ProgressNoteDraft:
    """Build a SOAP draft from active events with window_from <= occurred_at < window_to."""
    try:
        for name, ts in (("window_from", window_from), ("window_to", window_to), ("generated_at", generated_at)):
            require_aware(ts, name)
    except ValueError as e:
        raise ProgressNoteError(str(e)) from None
    if window_from >= window_to:
        raise ProgressNoteError("window_from must be before window_to")
    if not isinstance(generated_by, str) or not generated_by.strip():
        raise ProgressNoteError("generated_by is required")

    events = timeline.active_events(encounter_id, since=window_from, until=window_to)  # raises for unknown encounter
    conflicts = tuple(timeline.detect_conflicts(encounter_id, since=window_from, until=window_to))
    in_conflict: Dict[str, Conflict] = {eid: c for c in conflicts for eid in c.event_ids}

    local = [e.occurred_at.astimezone(IST) for e in events if e.category == EventCategory.VITAL]
    multi_day = len({d.date() for d in local}) > 1

    def stamp(e: Event) -> str:
        t = e.occurred_at.astimezone(IST)
        return t.strftime("%d-%b %H:%M ") if multi_day else t.strftime("%H:%M ")

    buckets: Dict[str, List[NoteLine]] = {"S": [], "O": [], "A": [], "P": []}
    omitted: List[dict] = []

    def add(section: str, e: Event, text: Optional[str], merge: bool):
        if text is None:
            omitted.append({"event_id": e.id, "category": e.category.value, "subtype": e.subtype,
                            "reason": "event lacks the fields needed to render a line"})
            return
        review = e.id in in_conflict
        lines = buckets[section]
        if merge:  # identical statement from several events -> one line citing all of them
            for i, l in enumerate(lines):
                if l.text == text:
                    lines[i] = NoteLine(text=text, event_ids=l.event_ids + (e.id,),
                                        color=GREEN if l.color == GREEN and _color(e) == GREEN else YELLOW,
                                        needs_review=l.needs_review or review,
                                        review_reason=REVIEW_CONFLICT if (l.needs_review or review) else "")
                    return
        lines.append(NoteLine(text=text, event_ids=(e.id,), color=_color(e), needs_review=review,
                              review_reason=REVIEW_CONFLICT if review else ""))

    for e in events:  # already chronological (occurred_at, then insertion)
        if e.category == EventCategory.SYMPTOM:
            add("S", e, _symptom(e), merge=True)
        elif e.category == EventCategory.VITAL:
            add("O", e, _vital(e, stamp(e)), merge=False)  # every reading kept, in time order
        elif e.category == EventCategory.DIAGNOSIS:
            add("A", e, _diagnosis(e), merge=True)
        elif e.category == EventCategory.PLAN:
            add("P", e, _plan(e), merge=True)
        elif e.category == EventCategory.MEDICATION_ORDER:
            add("P", e, _medication(e), merge=True)
        # other categories (ADT, ALLERGY, ...) are not part of the P1 SOAP note

    sections = []
    for key, title, required in SECTIONS:
        lines = buckets[key]
        if not lines:
            color, text = PLACEHOLDERS[key]
            lines = [NoteLine(text=text, event_ids=(), kind=PLACEHOLDER, color=color)]
        sections.append(NoteSection(key=key, title=title, required=required, lines=tuple(lines)))

    used = sorted({eid for s in sections for l in s.lines for eid in l.event_ids})
    return ProgressNoteDraft(encounter_id=encounter_id, window_from=window_from, window_to=window_to,
                             generated_at=generated_at, generated_by=generated_by.strip(),
                             sections=tuple(sections), source_event_ids=tuple(used),
                             conflicts=tuple(c for c in conflicts if set(c.event_ids) & set(used)),
                             omitted=tuple(omitted))
