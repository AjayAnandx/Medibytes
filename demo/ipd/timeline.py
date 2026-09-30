"""IPD P1 step 4: Patient Event Timeline service (over IpdStore).

One chronological, append-only clinical history per IPD encounter.

- append / append_many: validate, then insert atomically with audit.
  Re-appending an identical event (same id, same facts) is a no-op reported
  as a duplicate; the same id with different facts is rejected.
- correct: new Event with supersedes_event_id + reason; original untouched.
- retract: new retraction Event referring to the target; original untouched.
- history: full history or the active view (superseded / retracted events
  and retraction markers removed), with since/until/category/verification.
- detect_conflicts: flags differing values for the same fact at the same
  time. It never resolves them and never writes anything.

No clinical inference, no LLM, no deletion, no mutation of events.
"""
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models import (
    AuditAction, AuditEntry, EncounterStatus, Event, EventCategory, Role, SourceType, Verification, now,
    require_aware,
)
from .store import IpdStore

ACTIVE_ENCOUNTER_STATUSES = frozenset({EncounterStatus.ADMITTED, EncounterStatus.IN_WARD,
                                       EncounterStatus.IN_ICU, EncounterStatus.DISCHARGE_PLANNED})

# Known subtypes for the categories P1 produces; other categories accept any snake_case subtype.
KNOWN_SUBTYPES = {
    EventCategory.ADT: {"admitted", "transferred", "transferred_to_icu", "transferred_to_ward",
                        "discharge_decision", "discharge_cancelled", "discharged"},
    EventCategory.SYMPTOM: {"present", "denied"},
    EventCategory.VITAL: {"bp", "temperature", "spo2", "pulse", "rr", "map", "gcs", "pain_score"},
    EventCategory.DIAGNOSIS: {"provisional", "confirmed", "ruled_out"},
    EventCategory.MEDICATION_ORDER: {"proposed", "started", "modified", "stopped"},
    EventCategory.ALLERGY: {"active", "denied"},
    EventCategory.PLAN: {"followup", "plan"},
}
_SUBTYPE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Payload fields that carry the clinical value (evidence/diagnostic fields are ignored).
_VITAL_VALUE_FIELDS = {"bp": ("systolic", "diastolic")}
_DEFAULT_VITAL_FIELDS = ("value", "unit")


class TimelineError(ValueError):
    """Validation failure; nothing from the failing operation was persisted."""


@dataclass(frozen=True)
class AppendResult:
    appended: Tuple[Event, ...]
    duplicates: Tuple[str, ...]  # ids already stored with identical facts (not re-inserted)


@dataclass(frozen=True)
class Conflict:
    """Differing values for one fact at (about) the same time. Not resolved."""
    category: EventCategory
    key: str  # vital subtype, symptom term, allergy substance or drug name
    occurred_from: datetime
    occurred_to: datetime
    event_ids: Tuple[str, ...]
    values: Tuple[str, ...]  # canonical JSON of each event's value, same order as event_ids


def _facts(e: Event) -> Event:
    """Event with recorded_at neutralised, for idempotency comparison."""
    return replace(e, recorded_at=e.occurred_at)


class Timeline:
    def __init__(self, store: IpdStore):
        self.store = store

    # ------------------------------------------------------------ validation
    def _active_encounter(self, encounter_id: str):
        enc = self.store.get_encounter(encounter_id)
        if enc is None:
            raise TimelineError(f"unknown encounter {encounter_id}")
        if enc.status not in ACTIVE_ENCOUNTER_STATUSES:
            raise TimelineError(f"encounter {encounter_id} is not active ({enc.status.value})")
        return enc

    def _validate(self, e: Event, encounter_id: str) -> None:
        if not isinstance(e, Event):
            raise TimelineError(f"expected Event, got {type(e).__name__}")
        if e.encounter_id != encounter_id:
            raise TimelineError(f"event {e.id} belongs to {e.encounter_id}, not {encounter_id}")
        for name in ("id", "subtype", "author_id"):
            if not isinstance(getattr(e, name), str) or not getattr(e, name):
                raise TimelineError(f"event field {name} is required")
        if not isinstance(e.category, EventCategory) or not isinstance(e.author_role, Role) \
                or not isinstance(e.source_type, SourceType) or not isinstance(e.verification, Verification):
            raise TimelineError("event enums are invalid")
        try:
            require_aware(e.occurred_at, "occurred_at")
            require_aware(e.recorded_at, "recorded_at")
        except ValueError as err:
            raise TimelineError(str(err)) from None
        known = KNOWN_SUBTYPES.get(e.category)
        if not _SUBTYPE_RE.match(e.subtype) or (known is not None and e.subtype not in known):
            raise TimelineError(f"invalid subtype {e.subtype!r} for {e.category.value}")
        if isinstance(e.confidence, bool) or not isinstance(e.confidence, (int, float)) \
                or e.confidence != e.confidence or not 0.0 <= float(e.confidence) <= 1.0:
            raise TimelineError(f"invalid confidence {e.confidence!r}")
        if not isinstance(e.payload, dict) or not isinstance(e.source_span, dict) or not isinstance(e.codes, list):
            raise TimelineError("payload/source_span must be objects and codes a list")
        if e.source_capture_id is not None:
            cap = self.store.get_capture(e.source_capture_id)
            if cap is None or cap.encounter_id != encounter_id:
                raise TimelineError(f"source capture {e.source_capture_id} is not part of encounter {encounter_id}")
        if e.supersedes_event_id is not None:
            if not e.reason:
                raise TimelineError("a correction/retraction needs a reason")
            target = self.store.get_event(e.supersedes_event_id)
            if target is None or target.encounter_id != encounter_id:
                raise TimelineError(f"event {e.supersedes_event_id} is not part of encounter {encounter_id}")
            if target.retraction:
                raise TimelineError("a retraction marker cannot be corrected or retracted")
            if target.id in self.store.superseded_event_ids(encounter_id):
                raise TimelineError(f"event {target.id} was already corrected or retracted")
            if target.category != e.category:
                raise TimelineError("a correction must keep the original category")
        elif e.retraction:
            raise TimelineError("a retraction must reference the retracted event")

    # ------------------------------------------------------------ writes
    def _audit_for(self, e: Event) -> AuditEntry:
        if e.retraction:
            action, detail = AuditAction.RETRACT, {"retracts_event_id": e.supersedes_event_id, "reason": e.reason}
        elif e.supersedes_event_id:
            action, detail = AuditAction.SUPERSEDE, {"supersedes_event_id": e.supersedes_event_id, "reason": e.reason}
        else:
            action, detail = AuditAction.CREATE, {}
        detail.update({"encounter_id": e.encounter_id, "category": e.category.value, "subtype": e.subtype,
                       "source_capture_id": e.source_capture_id})
        return AuditEntry(user_id=e.author_id, role=e.author_role, action=action, entity="event",
                          entity_id=e.id, detail=detail, at=e.recorded_at)

    def append_many(self, encounter_id: str, events: Sequence[Event]) -> AppendResult:
        """Atomic: every event is appended (with audit) or none is."""
        events = list(events)
        appended, dups, batch = [], [], {}
        with self.store.transaction():
            self._active_encounter(encounter_id)
            for e in events:
                if not isinstance(e, Event):
                    raise TimelineError(f"expected Event, got {type(e).__name__}")
                if e.id in batch:
                    if _facts(batch[e.id]) != _facts(e):
                        raise TimelineError(f"event id {e.id} used twice with different facts")
                    continue
                existing = self.store.get_event(e.id)
                if existing is not None:
                    if _facts(existing) != _facts(e):
                        raise TimelineError(f"event id {e.id} already exists with different facts")
                    dups.append(e.id)
                    batch[e.id] = e
                    continue
                self._validate(e, encounter_id)
                self.store.append_event(e)
                self.store.append_audit(self._audit_for(e))
                appended.append(e)
                batch[e.id] = e
        return AppendResult(appended=tuple(appended), duplicates=tuple(dups))

    def append(self, event: Event) -> AppendResult:
        if not isinstance(event, Event):
            raise TimelineError(f"expected Event, got {type(event).__name__}")
        return self.append_many(event.encounter_id, [event])

    def correct(self, original_event_id: str, *, payload: dict, reason: str, author_id: str, author_role: Role,
                subtype: Optional[str] = None, occurred_at: Optional[datetime] = None,
                verification: Verification = Verification.VERIFIED, confidence: float = 1.0,
                source_type: SourceType = SourceType.TYPED, source_capture_id: Optional[str] = None,
                source_span: Optional[dict] = None, codes: Optional[list] = None,
                recorded_at: Optional[datetime] = None) -> Event:
        """Append a correcting Event; the original stays unchanged in history.

        Provenance of the correction is its own (the correcting clinician / capture);
        the original's evidence remains on the original event via the supersedes link.
        """
        original = self.store.get_event(original_event_id)
        if original is None:
            raise TimelineError(f"unknown event {original_event_id}")
        if not reason or not str(reason).strip():
            raise TimelineError("a correction needs a reason")
        new_subtype = subtype or original.subtype
        try:
            ev = Event(encounter_id=original.encounter_id, occurred_at=occurred_at or original.occurred_at,
                       recorded_at=recorded_at or now(), category=original.category, subtype=new_subtype,
                       payload=dict(payload), author_id=author_id, author_role=author_role,
                       source_type=source_type, confidence=confidence, verification=verification,
                       codes=list(codes) if codes is not None else (list(original.codes)
                                                                    if new_subtype == original.subtype else []),
                       source_capture_id=source_capture_id, source_span=dict(source_span or {}),
                       supersedes_event_id=original.id, reason=str(reason).strip())
        except ValueError as err:
            raise TimelineError(str(err)) from None
        self.append_many(original.encounter_id, [ev])
        return ev

    def retract(self, event_id: str, *, reason: str, author_id: str, author_role: Role,
                recorded_at: Optional[datetime] = None) -> Event:
        """Append a retraction marker for event_id; nothing is deleted."""
        target = self.store.get_event(event_id)
        if target is None:
            raise TimelineError(f"unknown event {event_id}")
        if not reason or not str(reason).strip():
            raise TimelineError("a retraction needs a reason")
        try:
            ev = Event(encounter_id=target.encounter_id, occurred_at=target.occurred_at,
                       recorded_at=recorded_at or now(), category=target.category, subtype=target.subtype,
                       payload={}, author_id=author_id, author_role=author_role, source_type=SourceType.TYPED,
                       confidence=1.0, verification=Verification.VERIFIED, supersedes_event_id=target.id,
                       reason=str(reason).strip(), retraction=True)
        except ValueError as err:
            raise TimelineError(str(err)) from None
        self.append_many(target.encounter_id, [ev])
        return ev

    # ------------------------------------------------------------ reads
    def _replaced_by(self, encounter_id: str) -> Dict[str, Event]:
        """target event id -> the event that corrected or retracted it."""
        return {e.supersedes_event_id: e for e in self.store.list_events(encounter_id) if e.supersedes_event_id}

    def history(self, encounter_id: str, *, since: Optional[datetime] = None, until: Optional[datetime] = None,
                categories: Optional[Iterable[EventCategory]] = None,
                verification: Optional[Iterable[Verification]] = None, active_only: bool = False) -> List[Event]:
        """Chronological events of one encounter (occurred_at, then insertion order).

        active_only drops superseded/retracted events and retraction markers.
        since is inclusive, until exclusive (on occurred_at).
        """
        if self.store.get_encounter(encounter_id) is None:
            raise TimelineError(f"unknown encounter {encounter_id}")
        events = self.store.list_events(encounter_id, since=since, until=until, categories=categories)
        if verification is not None:
            allowed = {Verification(v) for v in verification}
            events = [e for e in events if e.verification in allowed]
        if active_only:
            gone = self.store.superseded_event_ids(encounter_id)
            events = [e for e in events if e.id not in gone and not e.retraction]
        return events

    def active_events(self, encounter_id: str, **filters) -> List[Event]:
        return self.history(encounter_id, active_only=True, **filters)

    def status_of(self, event_id: str) -> str:
        """'active' | 'superseded' | 'retracted' | 'retraction' (a marker)."""
        e = self.store.get_event(event_id)
        if e is None:
            raise TimelineError(f"unknown event {event_id}")
        if e.retraction:
            return "retraction"
        by = self._replaced_by(e.encounter_id).get(e.id)
        if by is None:
            return "active"
        return "retracted" if by.retraction else "superseded"

    def correction_chain(self, event_id: str) -> List[Event]:
        """Original -> ... -> current head (or retraction marker) for the fact containing event_id."""
        e = self.store.get_event(event_id)
        if e is None:
            raise TimelineError(f"unknown event {event_id}")
        while e.supersedes_event_id:
            e = self.store.get_event(e.supersedes_event_id)
        chain, replaced = [e], self._replaced_by(e.encounter_id)
        while chain[-1].id in replaced:
            chain.append(replaced[chain[-1].id])
        return chain

    # ------------------------------------------------------------ conflicts (flag only)
    @staticmethod
    def _conflict_key(e: Event) -> Optional[Tuple[str, str]]:
        p = e.payload
        if e.category == EventCategory.VITAL:
            fields = _VITAL_VALUE_FIELDS.get(e.subtype, _DEFAULT_VITAL_FIELDS)
            return e.subtype, json.dumps({f: p.get(f) for f in fields}, sort_keys=True)
        if e.category == EventCategory.SYMPTOM and p.get("term"):
            return str(p["term"]).lower(), e.subtype
        if e.category == EventCategory.ALLERGY and p.get("substance"):
            return str(p["substance"]).lower(), e.subtype
        if e.category == EventCategory.MEDICATION_ORDER and p.get("name"):
            return str(p["name"]).lower(), json.dumps(
                {f: p.get(f) for f in ("dose", "unit", "frequency", "duration")}, sort_keys=True)
        return None  # diagnoses, plans, ADT: several at once are normal, not conflicts

    def detect_conflicts(self, encounter_id: str, *, window: timedelta = timedelta(0),
                         since: Optional[datetime] = None, until: Optional[datetime] = None) -> List[Conflict]:
        """Active events for the same fact within `window` of each other but with different values.

        Default window 0 = same occurred_at instant. Read-only; nothing is chosen or changed.
        """
        groups: Dict[Tuple[EventCategory, str], List[Tuple[Event, str]]] = {}
        for e in self.active_events(encounter_id, since=since, until=until):
            k = self._conflict_key(e)
            if k is not None:
                groups.setdefault((e.category, k[0]), []).append((e, k[1]))
        conflicts = []
        for (cat, key), items in sorted(groups.items(), key=lambda kv: (kv[0][0].value, kv[0][1])):
            cluster = [items[0]]
            for item in items[1:] + [None]:
                if item is not None and item[0].occurred_at - cluster[-1][0].occurred_at <= window:
                    cluster.append(item)
                    continue
                if len({v for _, v in cluster}) > 1:
                    conflicts.append(Conflict(category=cat, key=key, occurred_from=cluster[0][0].occurred_at,
                                              occurred_to=cluster[-1][0].occurred_at,
                                              event_ids=tuple(ev.id for ev, _ in cluster),
                                              values=tuple(v for _, v in cluster)))
                if item is not None:
                    cluster = [item]
        return conflicts
