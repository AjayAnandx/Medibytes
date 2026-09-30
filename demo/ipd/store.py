"""SQLite storage for IPD P1 (standard-library sqlite3 only).

- Default database is in-memory; a file path must be passed explicitly.
- events, source_records and audit_log are append-only, enforced by
  triggers (UPDATE/DELETE abort), not only by the absence of methods.
- document_versions: content, cited events and authorship are immutable;
  only stale / approval columns may change.
- Timestamps are stored as UTC ISO-8601 with microseconds so text order =
  time order; they are returned as timezone-aware datetimes.
- JSON columns use strict serialization (no NaN/Infinity, JSON types only).
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Iterable, List, Optional

from .models import (
    AuditEntry, BedAssignment, Capture, CaptureStatus, Document, DocumentStatus,
    DocumentVersion, Encounter, EncounterStatus, Event, EventCategory, Patient, Role,
    SourceRecord, require_aware,
)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id TEXT PRIMARY KEY,
    mrn TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    dob TEXT,
    sex TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS encounters (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    admit_at TEXT NOT NULL,
    discharge_at TEXT,
    linked_encounter_id TEXT REFERENCES encounters(id)
);
CREATE TABLE IF NOT EXISTS bed_assignments (
    id TEXT PRIMARY KEY,
    encounter_id TEXT NOT NULL REFERENCES encounters(id),
    ward TEXT NOT NULL,
    bed TEXT NOT NULL,
    unit_type TEXT NOT NULL,
    from_at TEXT NOT NULL,
    to_at TEXT
);
CREATE TABLE IF NOT EXISTS captures (
    id TEXT PRIMARY KEY,
    encounter_id TEXT NOT NULL REFERENCES encounters(id),
    source TEXT NOT NULL,
    capture_context TEXT NOT NULL,
    author_id TEXT NOT NULL,
    author_role TEXT NOT NULL,
    location TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    raw_uri TEXT,
    status TEXT NOT NULL,
    pipeline_info TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_records (
    capture_id TEXT PRIMARY KEY REFERENCES captures(id),
    text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    segments TEXT NOT NULL,
    engine TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    encounter_id TEXT NOT NULL REFERENCES encounters(id),
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    category TEXT NOT NULL,
    subtype TEXT NOT NULL,
    payload TEXT NOT NULL,
    codes TEXT NOT NULL,
    author_id TEXT NOT NULL,
    author_role TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_capture_id TEXT REFERENCES captures(id),
    source_span TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    verification TEXT NOT NULL,
    supersedes_event_id TEXT REFERENCES events(id),
    reason TEXT NOT NULL,
    retraction INTEGER NOT NULL CHECK (retraction IN (0, 1))
);
CREATE INDEX IF NOT EXISTS ix_events_encounter_time ON events(encounter_id, occurred_at);
CREATE INDEX IF NOT EXISTS ix_events_supersedes ON events(supersedes_event_id);
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    encounter_id TEXT NOT NULL REFERENCES encounters(id),
    type TEXT NOT NULL,
    window_from TEXT,
    window_to TEXT,
    current_version INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_versions (
    document_id TEXT NOT NULL REFERENCES documents(id),
    version INTEGER NOT NULL CHECK (version >= 1),
    content TEXT NOT NULL,
    source_event_ids TEXT NOT NULL,
    generator TEXT NOT NULL,
    change_type TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    stale INTEGER NOT NULL DEFAULT 0 CHECK (stale IN (0, 1)),
    approved_by TEXT,
    approved_role TEXT,
    approved_at TEXT,
    PRIMARY KEY (document_id, version)
);
CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    at TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_entity ON audit_log(entity, entity_id);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_supersede_same_encounter BEFORE INSERT ON events
WHEN NEW.supersedes_event_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM events WHERE id = NEW.supersedes_event_id AND encounter_id = NEW.encounter_id)
BEGIN SELECT RAISE(ABORT, 'superseded event must exist in the same encounter'); END;
CREATE TRIGGER IF NOT EXISTS source_records_no_update BEFORE UPDATE ON source_records
BEGIN SELECT RAISE(ABORT, 'source records are immutable'); END;
CREATE TRIGGER IF NOT EXISTS source_records_no_delete BEFORE DELETE ON source_records
BEGIN SELECT RAISE(ABORT, 'source records are immutable'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS document_versions_immutable
BEFORE UPDATE OF document_id, version, content, source_event_ids, generator, change_type,
                 created_by, created_at ON document_versions
BEGIN SELECT RAISE(ABORT, 'document version content is immutable'); END;
CREATE TRIGGER IF NOT EXISTS document_versions_no_delete BEFORE DELETE ON document_versions
BEGIN SELECT RAISE(ABORT, 'document versions are never deleted'); END;
"""


# ---------------------------------------------------------------- serialization
def _ts(value: Optional[datetime], name: str = "timestamp", optional: bool = True) -> Optional[str]:
    if value is None and optional:
        return None
    require_aware(value, name)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _dt(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def _json(value, name: str) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name} is not JSON-serializable: {e}") from None


def _unjson(value: str):
    return json.loads(value)


class IpdStore:
    """Repository over one SQLite connection. Use ':memory:' (default) for tests."""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._in_tx = False
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        with self._conn:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # ---- lifecycle
    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @contextmanager
    def transaction(self):
        """Group several writes; commits on success, rolls back on error."""
        if self._in_tx:
            yield self  # nested: the outer transaction decides
            return
        self._in_tx = True
        try:
            with self._conn:
                yield self
        finally:
            self._in_tx = False

    def _write(self, sql: str, params: tuple):
        if self._in_tx:
            return self._conn.execute(sql, params)
        with self._conn:
            return self._conn.execute(sql, params)

    def _one(self, sql: str, params: tuple):
        return self._conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple = ()):
        return self._conn.execute(sql, params).fetchall()

    # ---- patients
    def add_patient(self, p: Patient) -> Patient:
        self._write("INSERT INTO patients (id, mrn, name, dob, sex) VALUES (?, ?, ?, ?, ?)",
                    (p.id, p.mrn, p.name, p.dob.isoformat() if p.dob else None, p.sex))
        return p

    def get_patient(self, patient_id: str) -> Optional[Patient]:
        r = self._one("SELECT * FROM patients WHERE id = ?", (patient_id,))
        return None if r is None else Patient(
            id=r["id"], mrn=r["mrn"], name=r["name"],
            dob=date.fromisoformat(r["dob"]) if r["dob"] else None, sex=r["sex"])

    def get_patient_by_mrn(self, mrn: str) -> Optional[Patient]:
        r = self._one("SELECT id FROM patients WHERE mrn = ?", (mrn,))
        return None if r is None else self.get_patient(r["id"])

    # ---- encounters
    def add_encounter(self, e: Encounter) -> Encounter:
        self._write("INSERT INTO encounters (id, patient_id, type, status, admit_at, discharge_at, "
                    "linked_encounter_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (e.id, e.patient_id, e.type.value, e.status.value, _ts(e.admit_at, "admit_at", False),
                     _ts(e.discharge_at), e.linked_encounter_id))
        return e

    def get_encounter(self, encounter_id: str) -> Optional[Encounter]:
        r = self._one("SELECT * FROM encounters WHERE id = ?", (encounter_id,))
        return None if r is None else self._encounter(r)

    def list_encounters(self, patient_id: str) -> List[Encounter]:
        return [self._encounter(r) for r in
                self._all("SELECT * FROM encounters WHERE patient_id = ? ORDER BY admit_at", (patient_id,))]

    def set_encounter_status(self, encounter_id: str, status: EncounterStatus,
                             discharge_at: Optional[datetime] = None) -> None:
        status = EncounterStatus(status)
        cur = self._write("UPDATE encounters SET status = ?, discharge_at = COALESCE(?, discharge_at) "
                          "WHERE id = ?", (status.value, _ts(discharge_at, "discharge_at"), encounter_id))
        if cur.rowcount != 1:
            raise KeyError(f"unknown encounter {encounter_id}")

    @staticmethod
    def _encounter(r) -> Encounter:
        return Encounter(id=r["id"], patient_id=r["patient_id"], type=r["type"], status=r["status"],
                         admit_at=_dt(r["admit_at"]), discharge_at=_dt(r["discharge_at"]),
                         linked_encounter_id=r["linked_encounter_id"])

    # ---- bed assignments
    def add_bed_assignment(self, b: BedAssignment) -> BedAssignment:
        self._write("INSERT INTO bed_assignments (id, encounter_id, ward, bed, unit_type, from_at, to_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (b.id, b.encounter_id, b.ward, b.bed, b.unit_type.value,
                     _ts(b.from_at, "from_at", False), _ts(b.to_at)))
        return b

    def close_bed_assignment(self, bed_assignment_id: str, to_at: datetime) -> None:
        cur = self._write("UPDATE bed_assignments SET to_at = ? WHERE id = ? AND to_at IS NULL",
                          (_ts(to_at, "to_at", False), bed_assignment_id))
        if cur.rowcount != 1:
            raise KeyError(f"no open bed assignment {bed_assignment_id}")

    def list_bed_assignments(self, encounter_id: str) -> List[BedAssignment]:
        return [self._bed(r) for r in self._all(
            "SELECT * FROM bed_assignments WHERE encounter_id = ? ORDER BY from_at", (encounter_id,))]

    def get_active_bed(self, encounter_id: str) -> Optional[BedAssignment]:
        r = self._one("SELECT * FROM bed_assignments WHERE encounter_id = ? AND to_at IS NULL "
                      "ORDER BY from_at DESC LIMIT 1", (encounter_id,))
        return None if r is None else self._bed(r)

    @staticmethod
    def _bed(r) -> BedAssignment:
        return BedAssignment(id=r["id"], encounter_id=r["encounter_id"], ward=r["ward"], bed=r["bed"],
                             unit_type=r["unit_type"], from_at=_dt(r["from_at"]), to_at=_dt(r["to_at"]))

    # ---- captures + source records
    def add_capture(self, c: Capture) -> Capture:
        self._write("INSERT INTO captures (id, encounter_id, source, capture_context, author_id, author_role, "
                    "location, captured_at, raw_uri, status, pipeline_info) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (c.id, c.encounter_id, c.source.value, c.capture_context.value, c.author_id,
                     c.author_role.value, _json(c.location, "location"), _ts(c.captured_at, "captured_at", False),
                     c.raw_uri, c.status.value, _json(c.pipeline_info, "pipeline_info")))
        return c

    def set_capture_status(self, capture_id: str, status: CaptureStatus,
                           pipeline_info: Optional[dict] = None) -> None:
        status = CaptureStatus(status)
        info = None if pipeline_info is None else _json(pipeline_info, "pipeline_info")
        cur = self._write("UPDATE captures SET status = ?, pipeline_info = COALESCE(?, pipeline_info) WHERE id = ?",
                          (status.value, info, capture_id))
        if cur.rowcount != 1:
            raise KeyError(f"unknown capture {capture_id}")

    def get_capture(self, capture_id: str) -> Optional[Capture]:
        r = self._one("SELECT * FROM captures WHERE id = ?", (capture_id,))
        return None if r is None else Capture(
            id=r["id"], encounter_id=r["encounter_id"], source=r["source"], capture_context=r["capture_context"],
            author_id=r["author_id"], author_role=r["author_role"], location=_unjson(r["location"]),
            captured_at=_dt(r["captured_at"]), raw_uri=r["raw_uri"], status=r["status"],
            pipeline_info=_unjson(r["pipeline_info"]))

    def add_source_record(self, s: SourceRecord) -> SourceRecord:
        self._write("INSERT INTO source_records (capture_id, text, normalized_text, segments, engine) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (s.capture_id, s.text, s.normalized_text, _json(s.segments, "segments"), s.engine))
        return s

    def get_source_record(self, capture_id: str) -> Optional[SourceRecord]:
        r = self._one("SELECT * FROM source_records WHERE capture_id = ?", (capture_id,))
        return None if r is None else SourceRecord(
            capture_id=r["capture_id"], text=r["text"], normalized_text=r["normalized_text"],
            segments=_unjson(r["segments"]), engine=r["engine"])

    # ---- events (append-only)
    def append_event(self, e: Event) -> Event:
        self._write(
            "INSERT INTO events (id, encounter_id, occurred_at, recorded_at, category, subtype, payload, codes, "
            "author_id, author_role, source_type, source_capture_id, source_span, confidence, verification, "
            "supersedes_event_id, reason, retraction) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (e.id, e.encounter_id, _ts(e.occurred_at, "occurred_at", False), _ts(e.recorded_at, "recorded_at", False),
             e.category.value, e.subtype, _json(e.payload, "payload"), _json(e.codes, "codes"), e.author_id,
             e.author_role.value, e.source_type.value, e.source_capture_id, _json(e.source_span, "source_span"),
             float(e.confidence), e.verification.value, e.supersedes_event_id, e.reason, int(e.retraction)))
        return e

    def get_event(self, event_id: str) -> Optional[Event]:
        r = self._one("SELECT * FROM events WHERE id = ?", (event_id,))
        return None if r is None else self._event(r)

    def list_events(self, encounter_id: str, since: Optional[datetime] = None, until: Optional[datetime] = None,
                    categories: Optional[Iterable[EventCategory]] = None) -> List[Event]:
        """All recorded events (including superseded ones), ordered by occurred_at then insertion.

        since is inclusive, until is exclusive. Active/superseded status is derived
        by callers from superseded_event_ids().
        """
        sql, params = "SELECT * FROM events WHERE encounter_id = ?", [encounter_id]
        if since is not None:
            sql += " AND occurred_at >= ?"
            params.append(_ts(since, "since", False))
        if until is not None:
            sql += " AND occurred_at < ?"
            params.append(_ts(until, "until", False))
        if categories is not None:
            cats = [EventCategory(c).value for c in categories]
            if not cats:
                return []
            sql += f" AND category IN ({','.join('?' * len(cats))})"
            params.extend(cats)
        sql += " ORDER BY occurred_at, seq"
        return [self._event(r) for r in self._all(sql, tuple(params))]

    def superseded_event_ids(self, encounter_id: str) -> set:
        """IDs of events that a later event supersedes or retracts."""
        return {r[0] for r in self._all(
            "SELECT DISTINCT supersedes_event_id FROM events WHERE encounter_id = ? "
            "AND supersedes_event_id IS NOT NULL", (encounter_id,))}

    @staticmethod
    def _event(r) -> Event:
        return Event(
            id=r["id"], encounter_id=r["encounter_id"], occurred_at=_dt(r["occurred_at"]),
            recorded_at=_dt(r["recorded_at"]), category=r["category"], subtype=r["subtype"],
            payload=_unjson(r["payload"]), codes=_unjson(r["codes"]), author_id=r["author_id"],
            author_role=r["author_role"], source_type=r["source_type"], source_capture_id=r["source_capture_id"],
            source_span=_unjson(r["source_span"]), confidence=r["confidence"], verification=r["verification"],
            supersedes_event_id=r["supersedes_event_id"], reason=r["reason"], retraction=bool(r["retraction"]))

    # ---- documents + versions
    def add_document(self, d: Document) -> Document:
        self._write("INSERT INTO documents (id, encounter_id, type, window_from, window_to, current_version, "
                    "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (d.id, d.encounter_id, d.type.value, _ts(d.window_from), _ts(d.window_to),
                     d.current_version, d.status.value, _ts(d.created_at, "created_at", False)))
        return d

    def update_document(self, document_id: str, current_version: int, status: DocumentStatus,
                        window_to: Optional[datetime] = None) -> None:
        status = DocumentStatus(status)
        cur = self._write("UPDATE documents SET current_version = ?, status = ?, window_to = COALESCE(?, window_to) "
                          "WHERE id = ?", (int(current_version), status.value, _ts(window_to), document_id))
        if cur.rowcount != 1:
            raise KeyError(f"unknown document {document_id}")

    def get_document(self, document_id: str) -> Optional[Document]:
        r = self._one("SELECT * FROM documents WHERE id = ?", (document_id,))
        return None if r is None else self._document(r)

    def list_documents(self, encounter_id: str, doc_type=None) -> List[Document]:
        sql, params = "SELECT * FROM documents WHERE encounter_id = ?", [encounter_id]
        if doc_type is not None:
            sql += " AND type = ?"
            params.append(getattr(doc_type, "value", doc_type))
        return [self._document(r) for r in self._all(sql + " ORDER BY created_at", tuple(params))]

    @staticmethod
    def _document(r) -> Document:
        return Document(id=r["id"], encounter_id=r["encounter_id"], type=r["type"], window_from=_dt(r["window_from"]),
                        window_to=_dt(r["window_to"]), current_version=r["current_version"], status=r["status"],
                        created_at=_dt(r["created_at"]))

    def add_document_version(self, v: DocumentVersion) -> DocumentVersion:
        self._write("INSERT INTO document_versions (document_id, version, content, source_event_ids, generator, "
                    "change_type, created_by, created_at, stale, approved_by, approved_role, approved_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (v.document_id, v.version, _json(v.content, "content"),
                     _json(v.source_event_ids, "source_event_ids"), v.generator, v.change_type.value, v.created_by,
                     _ts(v.created_at, "created_at", False), int(v.stale), v.approved_by,
                     v.approved_role.value if v.approved_role else None, _ts(v.approved_at)))
        return v

    def get_document_version(self, document_id: str, version: int) -> Optional[DocumentVersion]:
        r = self._one("SELECT * FROM document_versions WHERE document_id = ? AND version = ?",
                      (document_id, int(version)))
        return None if r is None else self._version(r)

    def list_document_versions(self, document_id: str) -> List[DocumentVersion]:
        return [self._version(r) for r in self._all(
            "SELECT * FROM document_versions WHERE document_id = ? ORDER BY version", (document_id,))]

    def mark_version_stale(self, document_id: str, version: int) -> None:
        cur = self._write("UPDATE document_versions SET stale = 1 WHERE document_id = ? AND version = ?",
                          (document_id, int(version)))
        if cur.rowcount != 1:
            raise KeyError(f"unknown document version {document_id} v{version}")

    def record_version_approval(self, document_id: str, version: int, approved_by: str,
                                approved_role: Role, approved_at: datetime) -> None:
        """Set approval fields once; an already-approved version cannot be re-approved."""
        cur = self._write("UPDATE document_versions SET approved_by = ?, approved_role = ?, approved_at = ? "
                          "WHERE document_id = ? AND version = ? AND approved_at IS NULL",
                          (approved_by, Role(approved_role).value, _ts(approved_at, "approved_at", False),
                           document_id, int(version)))
        if cur.rowcount != 1:
            raise KeyError(f"document version {document_id} v{version} not found or already approved")

    @staticmethod
    def _version(r) -> DocumentVersion:
        return DocumentVersion(
            document_id=r["document_id"], version=r["version"], content=_unjson(r["content"]),
            source_event_ids=_unjson(r["source_event_ids"]), generator=r["generator"],
            change_type=r["change_type"], created_by=r["created_by"], created_at=_dt(r["created_at"]),
            stale=bool(r["stale"]), approved_by=r["approved_by"], approved_role=r["approved_role"],
            approved_at=_dt(r["approved_at"]))

    # ---- audit (append-only)
    def append_audit(self, a: AuditEntry) -> AuditEntry:
        self._write("INSERT INTO audit_log (id, at, user_id, role, action, entity, entity_id, detail) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (a.id, _ts(a.at, "at", False), a.user_id, a.role.value, a.action.value, a.entity,
                     a.entity_id, _json(a.detail, "detail")))
        return a

    def list_audit(self, entity: Optional[str] = None, entity_id: Optional[str] = None) -> List[AuditEntry]:
        sql, params = "SELECT * FROM audit_log WHERE 1 = 1", []
        if entity is not None:
            sql += " AND entity = ?"
            params.append(entity)
        if entity_id is not None:
            sql += " AND entity_id = ?"
            params.append(entity_id)
        return [AuditEntry(id=r["id"], at=_dt(r["at"]), user_id=r["user_id"], role=r["role"],
                           action=r["action"], entity=r["entity"], entity_id=r["entity_id"],
                           detail=_unjson(r["detail"]))
                for r in self._all(sql + " ORDER BY seq", tuple(params))]
