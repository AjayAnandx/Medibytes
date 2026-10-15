"""IPD P1 data models: plain dataclasses + string enums.

Timestamps are timezone-aware datetimes (naive values are rejected).
Events are frozen: a recorded clinical fact is never edited in place; a
correction is a new Event that supersedes the old one.
"""
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30), "IST")


def now() -> datetime:
    """Current time, timezone-aware (IST)."""
    return datetime.now(IST)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def require_aware(value: Optional[datetime], name: str, optional: bool = False) -> Optional[datetime]:
    if value is None and optional:
        return None
    if not isinstance(value, datetime):
        raise ValueError(f"{name} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _enum(cls, value, name):
    try:
        return value if isinstance(value, cls) else cls(value)
    except ValueError:
        raise ValueError(f"invalid {name}: {value!r}") from None


# ---------------------------------------------------------------- enums
class EncounterType(str, Enum):
    IPD = "IPD"
    OPD = "OPD"
    ER = "ER"


class EncounterStatus(str, Enum):
    PLANNED = "PLANNED"
    ADMITTED = "ADMITTED"
    IN_WARD = "IN_WARD"
    IN_ICU = "IN_ICU"
    DISCHARGE_PLANNED = "DISCHARGE_PLANNED"
    DISCHARGED = "DISCHARGED"


class UnitType(str, Enum):
    WARD = "WARD"
    ICU = "ICU"
    HDU = "HDU"


class Role(str, Enum):
    CONSULTANT = "CONSULTANT"
    RESIDENT = "RESIDENT"
    WARD_NURSE = "WARD_NURSE"
    ICU_NURSE = "ICU_NURSE"
    INTENSIVIST = "INTENSIVIST"
    PHARMACIST = "PHARMACIST"
    ADMIN = "ADMIN"
    DEVICE = "DEVICE"
    HIS = "HIS"
    SYSTEM = "SYSTEM"


class CaptureSource(str, Enum):
    AUDIO = "audio"
    TEXT = "text"
    STRUCTURED_FORM = "structured_form"
    DOCUMENT_IMAGE = "document_image"
    DEVICE = "device"
    HIS = "his"


class CaptureContext(str, Enum):
    WARD_ROUND = "ward_round"
    NURSING_ASSESSMENT = "nursing_assessment"
    ICU_HOURLY = "icu_hourly"
    HANDOVER = "handover"
    MED_ORDER = "med_order"
    FREE_DICTATION = "free_dictation"
    DOCUMENT_UPLOAD = "document_upload"
    DEVICE_FEED = "device_feed"
    HIS_FEED = "his_feed"


class CaptureStatus(str, Enum):
    RECEIVED = "received"
    PROCESSED = "processed"
    FAILED = "failed"


class EventCategory(str, Enum):
    ADT = "ADT"
    SYMPTOM = "SYMPTOM"
    EXAM_FINDING = "EXAM_FINDING"
    VITAL = "VITAL"
    VENTILATION = "VENTILATION"
    FLUID_IO = "FLUID_IO"
    MEDICATION_ADMIN = "MEDICATION_ADMIN"
    MEDICATION_ORDER = "MEDICATION_ORDER"
    INVESTIGATION = "INVESTIGATION"
    PROCEDURE = "PROCEDURE"
    DIAGNOSIS = "DIAGNOSIS"
    ALLERGY = "ALLERGY"  # not in the plan's list; proposed in P1 analysis (R4)
    CLINICAL_EVENT = "CLINICAL_EVENT"
    NURSING_ASSESSMENT = "NURSING_ASSESSMENT"
    PLAN = "PLAN"
    TASK = "TASK"
    ACKNOWLEDGEMENT = "ACKNOWLEDGEMENT"
    CONFIRMATION = "CONFIRMATION"


class SourceType(str, Enum):
    SPOKEN = "spoken"
    TYPED = "typed"
    DOCUMENT = "document"
    DEVICE = "device"
    HIS = "his"
    DERIVED = "derived"


class Verification(str, Enum):
    AUTO = "auto"
    UNVERIFIED = "unverified"
    VERIFIED = "verified"


class DocumentType(str, Enum):
    PROGRESS_NOTE = "progress_note"


class DocumentStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    SUPERSEDED = "superseded"


class ChangeType(str, Enum):
    GENERATED = "generated"
    REGENERATED = "regenerated"
    WORDING_EDIT = "wording_edit"


class AuditAction(str, Enum):
    CREATE = "create"
    GENERATE = "generate"
    SUPERSEDE = "supersede"
    RETRACT = "retract"
    EDIT = "edit"
    APPROVE = "approve"
    EXPORT = "export"
    VIEW = "view"


# ---------------------------------------------------------------- entities
@dataclass
class Patient:
    mrn: str
    name: str
    dob: Optional[date] = None
    sex: str = ""
    id: str = field(default_factory=lambda: new_id("pat"))

    def __post_init__(self):
        if not self.mrn:
            raise ValueError("mrn is required")
        if self.dob is not None and (not isinstance(self.dob, date) or isinstance(self.dob, datetime)):
            raise ValueError("dob must be a date")


@dataclass
class Encounter:
    patient_id: str
    admit_at: datetime
    type: EncounterType = EncounterType.IPD
    status: EncounterStatus = EncounterStatus.ADMITTED
    discharge_at: Optional[datetime] = None
    linked_encounter_id: Optional[str] = None
    id: str = field(default_factory=lambda: new_id("enc"))

    def __post_init__(self):
        self.type = _enum(EncounterType, self.type, "encounter type")
        self.status = _enum(EncounterStatus, self.status, "encounter status")
        require_aware(self.admit_at, "admit_at")
        require_aware(self.discharge_at, "discharge_at", optional=True)


@dataclass
class BedAssignment:
    encounter_id: str
    ward: str
    bed: str
    from_at: datetime
    unit_type: UnitType = UnitType.WARD
    to_at: Optional[datetime] = None
    id: str = field(default_factory=lambda: new_id("bed"))

    def __post_init__(self):
        self.unit_type = _enum(UnitType, self.unit_type, "unit type")
        require_aware(self.from_at, "from_at")
        require_aware(self.to_at, "to_at", optional=True)


@dataclass
class Capture:
    encounter_id: str
    source: CaptureSource
    capture_context: CaptureContext
    author_id: str
    author_role: Role
    captured_at: datetime
    location: dict = field(default_factory=dict)  # {"ward", "bed", "unit_type"}
    raw_uri: Optional[str] = None
    status: CaptureStatus = CaptureStatus.RECEIVED
    pipeline_info: dict = field(default_factory=dict)  # engine, llm_engine, ...
    id: str = field(default_factory=lambda: new_id("cap"))

    def __post_init__(self):
        self.source = _enum(CaptureSource, self.source, "capture source")
        self.capture_context = _enum(CaptureContext, self.capture_context, "capture context")
        self.author_role = _enum(Role, self.author_role, "author role")
        self.status = _enum(CaptureStatus, self.status, "capture status")
        require_aware(self.captured_at, "captured_at")
        if not self.author_id:
            raise ValueError("author_id is required")


@dataclass
class SourceRecord:
    """Text produced from a capture (transcript / typed text / OCR text)."""
    capture_id: str
    text: str
    normalized_text: str = ""
    segments: list = field(default_factory=list)
    engine: str = ""


@dataclass(frozen=True)
class Event:
    encounter_id: str
    occurred_at: datetime
    category: EventCategory
    subtype: str
    author_id: str
    author_role: Role
    source_type: SourceType
    payload: dict = field(default_factory=dict)
    confidence: float = 1.0
    verification: Verification = Verification.UNVERIFIED
    codes: list = field(default_factory=list)  # [{"system", "code", "display"}]
    source_capture_id: Optional[str] = None
    source_span: dict = field(default_factory=dict)  # segment_id, start_ms, end_ms, char_start, char_end, ocr_line
    supersedes_event_id: Optional[str] = None
    reason: str = ""
    retraction: bool = False  # True: withdraws supersedes_event_id without a replacement fact
    recorded_at: datetime = field(default_factory=now)
    id: str = field(default_factory=lambda: new_id("evt"))

    def __post_init__(self):
        object.__setattr__(self, "category", _enum(EventCategory, self.category, "event category"))
        object.__setattr__(self, "author_role", _enum(Role, self.author_role, "author role"))
        object.__setattr__(self, "source_type", _enum(SourceType, self.source_type, "source type"))
        object.__setattr__(self, "verification", _enum(Verification, self.verification, "verification"))
        require_aware(self.occurred_at, "occurred_at")
        require_aware(self.recorded_at, "recorded_at")
        if not self.subtype:
            raise ValueError("subtype is required")
        if not self.author_id:
            raise ValueError("author_id is required")
        if not isinstance(self.confidence, (int, float)) or not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if self.supersedes_event_id and not self.reason:
            raise ValueError("a superseding event needs a reason")
        if self.retraction and not self.supersedes_event_id:
            raise ValueError("a retraction must reference supersedes_event_id")


@dataclass
class Document:
    encounter_id: str
    type: DocumentType
    window_from: Optional[datetime] = None
    window_to: Optional[datetime] = None
    current_version: int = 0
    status: DocumentStatus = DocumentStatus.DRAFT
    created_at: datetime = field(default_factory=now)
    id: str = field(default_factory=lambda: new_id("doc"))

    def __post_init__(self):
        self.type = _enum(DocumentType, self.type, "document type")
        self.status = _enum(DocumentStatus, self.status, "document status")
        require_aware(self.window_from, "window_from", optional=True)
        require_aware(self.window_to, "window_to", optional=True)
        require_aware(self.created_at, "created_at")


@dataclass
class DocumentVersion:
    """One immutable version of a document. Only stale/approval fields change later."""
    document_id: str
    version: int
    content: dict
    source_event_ids: list
    generator: str
    created_by: str
    change_type: ChangeType = ChangeType.GENERATED
    created_at: datetime = field(default_factory=now)
    stale: bool = False
    approved_by: Optional[str] = None
    approved_role: Optional[Role] = None
    approved_at: Optional[datetime] = None

    def __post_init__(self):
        self.change_type = _enum(ChangeType, self.change_type, "change type")
        if self.approved_role is not None:
            self.approved_role = _enum(Role, self.approved_role, "approved role")
        if not isinstance(self.version, int) or self.version < 1:
            raise ValueError("version must be an integer >= 1")
        require_aware(self.created_at, "created_at")
        require_aware(self.approved_at, "approved_at", optional=True)


@dataclass
class AuditEntry:
    user_id: str
    role: Role
    action: AuditAction
    entity: str
    entity_id: str
    detail: dict = field(default_factory=dict)
    at: datetime = field(default_factory=now)
    id: str = field(default_factory=lambda: new_id("aud"))

    def __post_init__(self):
        self.role = _enum(Role, self.role, "role")
        self.action = _enum(AuditAction, self.action, "audit action")
        require_aware(self.at, "at")
