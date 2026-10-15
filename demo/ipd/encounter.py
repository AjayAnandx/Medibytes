"""IPD P1 step 2: admission (Encounter + BedAssignment + ADT event + audit).

admit_patient() performs one transactional admission:
  Patient (inserted if new) -> IPD Encounter (IN_WARD / IN_ICU)
  -> open BedAssignment -> ADT/admitted Event -> AuditEntry.
If any step fails, none of that admission's writes persist.

The ADT event carries only what was supplied at admission (ward, bed,
unit type, resulting status, linked encounter). No clinical content is
inferred or added.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .models import (
    AuditAction, AuditEntry, BedAssignment, Encounter, EncounterStatus, EncounterType, Event,
    EventCategory, Patient, Role, SourceType, UnitType, Verification, now, require_aware,
)
from .store import IpdStore

# Roles allowed to record an admission (admitting doctors, admission desk, HIS feed).
ADMITTING_ROLES = frozenset({Role.CONSULTANT, Role.RESIDENT, Role.INTENSIVIST, Role.ADMIN, Role.HIS})

# Where the patient is after admission, by the unit of the first bed.
_STATUS_FOR_UNIT = {UnitType.WARD: EncounterStatus.IN_WARD,
                    UnitType.ICU: EncounterStatus.IN_ICU,
                    UnitType.HDU: EncounterStatus.IN_ICU}


@dataclass(frozen=True)
class AdmissionResult:
    patient: Patient
    encounter: Encounter
    bed: BedAssignment
    event: Event
    audit: AuditEntry


def synthetic_patient(label: str = "001") -> Patient:
    """Clearly synthetic demo patient (no real identity)."""
    return Patient(mrn=f"DEMO-{label}", name=f"Demo Patient {label}")


def admit_patient(store: IpdStore, patient: Patient, *, ward: str, bed: str, admitted_by: str,
                  admitted_role: Role, unit_type: UnitType = UnitType.WARD,
                  admit_at: Optional[datetime] = None, linked_encounter_id: Optional[str] = None,
                  source_type: SourceType = SourceType.TYPED) -> AdmissionResult:
    """Admit `patient` to an IPD encounter in one transaction.

    `patient` is inserted unless a patient with the same id already exists.
    Raises ValueError for invalid input; storage errors propagate after rollback.
    """
    role = Role(admitted_role)
    unit = UnitType(unit_type)
    if role not in ADMITTING_ROLES:
        raise ValueError(f"role {role.value} cannot record an admission")
    if not admitted_by:
        raise ValueError("admitted_by is required")
    ward, bed = (ward or "").strip(), (bed or "").strip()
    if not ward or not bed:
        raise ValueError("ward and bed are required")
    admit_at = require_aware(admit_at if admit_at is not None else now(), "admit_at")
    status = _STATUS_FOR_UNIT[unit]

    encounter = Encounter(patient_id=patient.id, admit_at=admit_at, type=EncounterType.IPD,
                          status=status, linked_encounter_id=linked_encounter_id)
    bed_assignment = BedAssignment(encounter_id=encounter.id, ward=ward, bed=bed,
                                   unit_type=unit, from_at=admit_at)
    payload = {"ward": ward, "bed": bed, "unit_type": unit.value, "encounter_status": status.value,
               "bed_assignment_id": bed_assignment.id}
    if linked_encounter_id:
        payload["linked_encounter_id"] = linked_encounter_id
    event = Event(encounter_id=encounter.id, occurred_at=admit_at, category=EventCategory.ADT,
                  subtype="admitted", author_id=admitted_by, author_role=role, source_type=source_type,
                  payload=payload, confidence=1.0, verification=Verification.VERIFIED)
    audit = AuditEntry(user_id=admitted_by, role=role, action=AuditAction.CREATE, entity="encounter",
                       entity_id=encounter.id,
                       detail={"operation": "admission", "patient_id": patient.id,
                               "bed_assignment_id": bed_assignment.id, "event_id": event.id,
                               "ward": ward, "bed": bed, "unit_type": unit.value})

    with store.transaction():
        if store.get_patient(patient.id) is None:
            store.add_patient(patient)
        store.add_encounter(encounter)
        store.add_bed_assignment(bed_assignment)
        store.append_event(event)
        store.append_audit(audit)
    return AdmissionResult(patient=patient, encounter=encounter, bed=bed_assignment, event=event, audit=audit)
