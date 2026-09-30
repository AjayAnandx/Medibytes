"""IPD P1 step 7: progress-note Documents — persistence, versioning, staleness, approval.

Uses the existing IpdStore tables (documents, document_versions, audit_log),
the Timeline (read-only here) and ProgressNoteDraft. No LLM, no event writes.

Versions
  v1 generated from a ProgressNoteDraft; later versions are NEW rows:
  change_type=regenerated (timeline changed) or wording_edit (clinician).
  Version content is immutable (enforced by DB triggers); only the stale
  flag and one-time approval fields are ever set on a version.

Wording edits (deliberately restrictive — no semantic validator exists)
  Only the text of existing CLINICAL lines may change. Every word, number
  and unit of the generated line must remain, in order; the only words that
  may be added are neutral filler (ALLOWED_FILLER). Event ids, colours,
  review flags and placeholders cannot change. Clinical fact corrections go
  through Timeline.correct()/retract() and a regenerated version instead.

Staleness (deterministic, no timestamps involved)
  A version is stale if any event it covers (cited lines + omitted events)
  is no longer active or no longer exists, or if an active event of a note
  category inside the document window is not covered by the version.

Approval
  check_approval() lists every blocking problem; approve() refuses unless the
  list is empty, then records approver/role/time once and audits it.
"""
import copy
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .models import (
    AuditAction, AuditEntry, ChangeType, Document, DocumentStatus, DocumentType, DocumentVersion, EncounterStatus,
    EventCategory, Role, require_aware,
)
from .progress_note import CLINICAL, PLACEHOLDER, RED, ProgressNoteDraft, generate_progress_note
from .store import IpdStore
from .timeline import Timeline

APPROVER_ROLES = frozenset({Role.CONSULTANT, Role.RESIDENT, Role.INTENSIVIST})
EDITOR_ROLES = APPROVER_ROLES
# Encounter states in which a progress note may be approved (DISCHARGED is refused in P1).
REVIEWABLE_ENCOUNTER = frozenset({EncounterStatus.ADMITTED, EncounterStatus.IN_WARD, EncounterStatus.IN_ICU,
                                  EncounterStatus.DISCHARGE_PLANNED})
# Event categories the P1 progress-note generator reads (keep in sync with progress_note.py; tested).
NOTE_CATEGORIES = frozenset({EventCategory.SYMPTOM, EventCategory.VITAL, EventCategory.DIAGNOSIS,
                             EventCategory.PLAN, EventCategory.MEDICATION_ORDER})
PROPOSED_MED_PREFIX = "Proposed medication (not confirmed, not prescribed)"
ALLOWED_FILLER = frozenset({"patient", "reports", "reported", "noted", "recorded", "observed", "is", "was", "has",
                            "the", "a", "an", "at", "on", "of", "currently", "complains"})
_TOKEN = re.compile(r"[a-z0-9]+(?:[.:/][0-9]+)*|%")


class DocumentError(ValueError):
    """Operation refused; nothing was written."""


class ApprovalError(DocumentError):
    def __init__(self, problems):
        super().__init__("approval refused: " + "; ".join(problems))
        self.problems = tuple(problems)


@dataclass(frozen=True)
class StaleReport:
    stale: bool
    reasons: Tuple[str, ...]


@dataclass(frozen=True)
class ApprovalResult:
    document: Document
    version: DocumentVersion
    already_approved: bool = False


# ---------------------------------------------------------------- helpers
def _role(role, allowed, what) -> Role:
    r = Role(role)
    if r not in allowed:
        raise DocumentError(f"role {r.value} cannot {what}")
    return r


def _actor(actor_id: str) -> str:
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise DocumentError("actor id is required")
    return actor_id.strip()


def _audit(store, action, doc, actor, role, at, **detail):
    store.append_audit(AuditEntry(user_id=actor, role=role, action=action, entity="document", entity_id=doc.id,
                                  at=at, detail={"encounter_id": doc.encounter_id, "document_type": doc.type.value,
                                                 **detail}))


def _get(store, document_id) -> Document:
    doc = store.get_document(document_id)
    if doc is None:
        raise DocumentError(f"unknown document {document_id}")
    if doc.type != DocumentType.PROGRESS_NOTE:
        raise DocumentError(f"document {document_id} is not a progress note")
    return doc


def _current(store, doc) -> DocumentVersion:
    v = store.get_document_version(doc.id, doc.current_version)
    if v is None:
        raise DocumentError(f"document {doc.id} has no version {doc.current_version}")
    return v


def _lines(content: dict):
    for s in content.get("sections", []):
        for i, line in enumerate(s.get("lines", [])):
            yield s, i, line


def _covered(version: DocumentVersion) -> set:
    omitted = {o.get("event_id") for o in version.content.get("omitted", []) if o.get("event_id")}
    return set(version.source_event_ids) | omitted


def _check_draft(store, draft):
    if not isinstance(draft, ProgressNoteDraft):
        raise DocumentError("expected a ProgressNoteDraft")
    if draft.status != DocumentStatus.DRAFT:
        raise DocumentError("only DRAFT progress notes can be stored")
    if store.get_encounter(draft.encounter_id) is None:
        raise DocumentError(f"unknown encounter {draft.encounter_id}")


# ---------------------------------------------------------------- create / version
def create_progress_note_document(store: IpdStore, draft: ProgressNoteDraft, *, created_by: str,
                                  created_role: Role) -> Tuple[Document, DocumentVersion]:
    """Document (draft, v1) + DocumentVersion 1 + audit, in one transaction."""
    _check_draft(store, draft)
    actor, role = _actor(created_by), _role(created_role, EDITOR_ROLES, "create progress notes")
    doc = Document(encounter_id=draft.encounter_id, type=DocumentType.PROGRESS_NOTE, window_from=draft.window_from,
                   window_to=draft.window_to, current_version=1, status=DocumentStatus.DRAFT,
                   created_at=draft.generated_at)
    v1 = DocumentVersion(document_id=doc.id, version=1, content=draft.to_content(),
                         source_event_ids=list(draft.source_event_ids), generator=draft.generator, created_by=actor,
                         change_type=ChangeType.GENERATED, created_at=draft.generated_at, stale=False)
    with store.transaction():
        store.add_document(doc)
        store.add_document_version(v1)
        _audit(store, AuditAction.CREATE, doc, actor, role, draft.generated_at, version=1)
        _audit(store, AuditAction.GENERATE, doc, actor, role, draft.generated_at, version=1,
               change_type=ChangeType.GENERATED.value, n_source_events=len(v1.source_event_ids))
    return store.get_document(doc.id), store.get_document_version(doc.id, 1)


def regenerate_progress_note(store: IpdStore, document_id: str, *, generated_by: str, generated_role: Role,
                             generated_at: datetime, window_to: Optional[datetime] = None
                             ) -> Tuple[Document, DocumentVersion]:
    """Re-run the generator over the document window and store it as the next version."""
    doc = _get(store, document_id)
    actor, role = _actor(generated_by), _role(generated_role, EDITOR_ROLES, "regenerate progress notes")
    draft = generate_progress_note(Timeline(store), doc.encounter_id, window_from=doc.window_from,
                                   window_to=window_to or doc.window_to, generated_by=actor, generated_at=generated_at)
    _check_draft(store, draft)
    n = max(v.version for v in store.list_document_versions(doc.id)) + 1
    ver = DocumentVersion(document_id=doc.id, version=n, content=draft.to_content(),
                          source_event_ids=list(draft.source_event_ids), generator=draft.generator, created_by=actor,
                          change_type=ChangeType.REGENERATED, created_at=generated_at)
    with store.transaction():
        store.add_document_version(ver)
        store.update_document(doc.id, n, DocumentStatus.DRAFT, window_to=draft.window_to)
        _audit(store, AuditAction.GENERATE, doc, actor, role, generated_at, version=n, previous_version=n - 1,
               change_type=ChangeType.REGENERATED.value, n_source_events=len(ver.source_event_ids))
    return store.get_document(doc.id), store.get_document_version(doc.id, n)


def _wording_ok(original: str, new: str) -> Optional[str]:
    """None if `new` only adds neutral filler / punctuation / case to `original`."""
    old_t, new_t = _TOKEN.findall(original.lower()), _TOKEN.findall(new.lower())
    i = 0
    for tok in new_t:
        if i < len(old_t) and tok == old_t[i]:
            i += 1
        elif tok not in ALLOWED_FILLER:
            return f"word {tok!r} is not allowed in a wording edit"
    if i < len(old_t):
        return f"generated wording {old_t[i]!r} was removed or reordered"
    return None


def edit_wording(store: IpdStore, document_id: str, *, base_version: int, edits: Dict[Tuple[str, int], str],
                 edited_by: str, edited_role: Role, edited_at: datetime, reason: str = "") -> Tuple[Document, DocumentVersion]:
    """New version with clinician wording changes to existing clinical lines only.

    edits: {(section_key, line_index): new_text}. Refused if the base is not the
    current version, is stale, or any edit fails the conservative wording rule.
    """
    doc = _get(store, document_id)
    actor, role = _actor(edited_by), _role(edited_role, EDITOR_ROLES, "edit progress notes")
    try:
        require_aware(edited_at, "edited_at")
    except ValueError as e:
        raise DocumentError(str(e)) from None
    if base_version != doc.current_version:
        raise DocumentError(f"version {base_version} is not the current version ({doc.current_version})")
    base = _current(store, doc)
    rep = staleness(store, doc.id, base.version)
    if rep.stale:
        raise DocumentError("version is stale; regenerate from the timeline before editing: " + "; ".join(rep.reasons))
    if not edits:
        raise DocumentError("no wording edits supplied")
    content = copy.deepcopy(base.content)
    index = {(s["key"], i): line for s, i, line in _lines(content)}
    changed = []
    for (key, idx), new_text in sorted(edits.items()):
        line = index.get((key, idx))
        if line is None:
            raise DocumentError(f"no line {key}[{idx}]")
        if line.get("kind") != CLINICAL or not line.get("event_ids"):
            raise DocumentError(f"line {key}[{idx}] is a placeholder and cannot be edited")
        if not isinstance(new_text, str) or not new_text.strip():
            raise DocumentError(f"line {key}[{idx}] cannot be blank")
        generated = line.get("generated_text", line["text"])
        problem = _wording_ok(generated, new_text)
        if problem:
            raise DocumentError(f"line {key}[{idx}]: {problem}")
        if new_text.strip() != line["text"]:
            line.setdefault("generated_text", generated)
            line["text"] = new_text.strip()
            changed.append(f"{key}[{idx}]")
    if not changed:
        raise DocumentError("wording edit changes nothing")
    content["edited_from_version"] = base.version
    n = max(v.version for v in store.list_document_versions(doc.id)) + 1
    ver = DocumentVersion(document_id=doc.id, version=n, content=content, source_event_ids=list(base.source_event_ids),
                          generator=base.generator, created_by=actor, change_type=ChangeType.WORDING_EDIT,
                          created_at=edited_at)
    with store.transaction():
        store.add_document_version(ver)
        store.update_document(doc.id, n, DocumentStatus.DRAFT)
        _audit(store, AuditAction.EDIT, doc, actor, role, edited_at, version=n, previous_version=base.version,
               change_type=ChangeType.WORDING_EDIT.value, lines=changed, reason=reason)
    return store.get_document(doc.id), store.get_document_version(doc.id, n)


# ---------------------------------------------------------------- staleness
def staleness(store: IpdStore, document_id: str, version: Optional[int] = None) -> StaleReport:
    """Read-only, deterministic staleness check against the current timeline."""
    doc = _get(store, document_id)
    ver = store.get_document_version(doc.id, version or doc.current_version)
    if ver is None:
        raise DocumentError(f"unknown version {version}")
    tl, reasons = Timeline(store), []
    covered = _covered(ver)
    for eid in sorted(covered):
        e = store.get_event(eid)
        if e is None or e.encounter_id != doc.encounter_id:
            reasons.append(f"event {eid} is missing or belongs to another encounter")
            continue
        status = tl.status_of(eid)
        if status != "active":
            reasons.append(f"event {eid} is {status}")
    window = tl.active_events(doc.encounter_id, since=doc.window_from, until=doc.window_to,
                              categories=sorted(NOTE_CATEGORIES, key=lambda c: c.value))
    for e in window:
        if e.id not in covered:
            reasons.append(f"new {e.category.value}/{e.subtype} event {e.id} in the note window")
    if ver.stale and not reasons:
        reasons.append("version was marked stale")
    return StaleReport(stale=bool(reasons), reasons=tuple(reasons))


def mark_stale_if_needed(store: IpdStore, document_id: str) -> StaleReport:
    """Persist the stale flag on the current version when stale (never regenerates)."""
    doc = _get(store, document_id)
    rep = staleness(store, doc.id)
    ver = _current(store, doc)
    if rep.stale and not ver.stale and ver.approved_at is None:
        store.mark_version_stale(doc.id, ver.version)
    return rep


# ---------------------------------------------------------------- approval
def check_approval(store: IpdStore, document_id: str, version: int, approver_role: Role) -> List[str]:
    """Every reason the version cannot be approved (empty list = approvable)."""
    problems: List[str] = []
    doc = store.get_document(document_id)
    if doc is None:
        return [f"unknown document {document_id}"]
    if doc.type != DocumentType.PROGRESS_NOTE:
        problems.append("document is not a progress note")
    ver = store.get_document_version(doc.id, version)
    if ver is None:
        return problems + [f"version {version} does not exist"]
    try:
        if Role(approver_role) not in APPROVER_ROLES:
            problems.append(f"role {Role(approver_role).value} cannot approve progress notes")
    except ValueError:
        problems.append(f"unknown role {approver_role!r}")
    if version != doc.current_version:
        problems.append(f"version {version} is not the current version ({doc.current_version})")
    if ver.approved_at is not None:
        problems.append(f"version {version} is already approved")
    enc = store.get_encounter(doc.encounter_id)
    if enc is None or enc.status not in REVIEWABLE_ENCOUNTER:
        problems.append(f"encounter is not in a reviewable state ({enc.status.value if enc else 'missing'})")
    rep = staleness(store, doc.id, version)
    if rep.stale:
        problems.append("version is stale: " + "; ".join(rep.reasons))
    c, tl = ver.content, Timeline(store)
    if c.get("conflicts"):
        problems.append("unresolved conflicts in the note")
    cited = set()
    for s, i, line in _lines(c):
        ref = f"{s.get('key')}[{i}]"
        if line.get("needs_review"):
            problems.append(f"{ref} requires review (conflict)")
        if s.get("required") and line.get("color") == RED:
            problems.append(f"required section {s.get('key')} has no supported information")
        ids = line.get("event_ids") or []
        if line.get("kind") == PLACEHOLDER:
            if ids:
                problems.append(f"{ref} placeholder carries event ids")
            continue
        if line.get("kind") != CLINICAL or not ids:
            problems.append(f"{ref} clinical line has no source event")
            continue
        for eid in ids:
            cited.add(eid)
            e = store.get_event(eid)
            if e is None:
                problems.append(f"{ref} references missing event {eid}")
            elif e.encounter_id != doc.encounter_id:
                problems.append(f"{ref} references event {eid} from another encounter")
            elif tl.status_of(eid) != "active":
                problems.append(f"{ref} references {tl.status_of(eid)} event {eid}")
            elif e.category == EventCategory.MEDICATION_ORDER:
                if e.subtype != "proposed" or not str(line.get("text", "")).startswith(PROPOSED_MED_PREFIX):
                    problems.append(f"{ref} medication must read as proposed and not prescribed")
    if cited != set(ver.source_event_ids):
        problems.append("source_event_ids do not match the events cited by the lines")
    return list(dict.fromkeys(problems))  # stable de-duplication


def approve(store: IpdStore, document_id: str, *, version: int, approved_by: str, approved_role: Role,
            approved_at: datetime) -> ApprovalResult:
    """Explicit approval of one specific version. Refused (ApprovalError) on any problem."""
    actor = _actor(approved_by)
    try:
        require_aware(approved_at, "approved_at")
    except ValueError as e:
        raise DocumentError(str(e)) from None
    doc = store.get_document(document_id)
    ver = store.get_document_version(document_id, version) if doc else None
    if doc and ver and ver.approved_at is not None and version == doc.current_version \
            and doc.status == DocumentStatus.APPROVED:
        return ApprovalResult(document=doc, version=ver, already_approved=True)  # idempotent, nothing written
    problems = check_approval(store, document_id, version, approved_role)
    if problems:
        raise ApprovalError(problems)
    role = Role(approved_role)
    with store.transaction():
        store.record_version_approval(doc.id, version, actor, role, approved_at)
        store.update_document(doc.id, version, DocumentStatus.APPROVED)
        _audit(store, AuditAction.APPROVE, doc, actor, role, approved_at, version=version)
    return ApprovalResult(document=store.get_document(doc.id), version=store.get_document_version(doc.id, version))
