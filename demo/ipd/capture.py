"""IPD P1 step 5A: typed-text ward-round capture orchestration.

  typed text -> Capture(source=TEXT, context=WARD_ROUND) + SourceRecord
             -> existing stt_extract.run_text_extract()        (unchanged)
             -> ipd.event_extractor (entities -> Events)       (no mapping here)
             -> ipd.timeline.Timeline.append_many()             (atomic)

EXTRACTOR MODE ADAPTER — READ BEFORE REUSING
  run_text_extract has no "typed" source. Its source="image" mode is the
  safe deterministic mode for typed text: no speech-mishear rewrites (e.g.
  "by AC" -> "by 80"), no fuzzy drug-name matching, line breaks are
  boundaries, and with use_llm=False no Ollama/LLM runs. This module calls
  it with source="image", use_llm=False INTERNALLY ONLY. The IPD Capture is
  always source=TEXT and events are SourceType.TYPED. The mode is recorded in
  Capture.pipeline_info["extractor_source_mode"]; nothing downstream should
  treat typed text as an image.

Transactions and failures
  Success: Capture(PROCESSED) + SourceRecord + all Events + audit are written
  in ONE transaction. Any failure after validation rolls that back and then
  records the Capture as FAILED (with the typed text when storable and the
  error in pipeline_info) so the failure is visible and can be reprocessed.
  No clinical history is ever deleted. CaptureProcessingError is raised; a
  failed capture is never reported as success.

Idempotency
  Event ids are deterministic per capture, so reprocess_capture() (or calling
  capture_typed_ward_round again with the same capture_id) appends nothing new.
"""
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Tuple

_DEMO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _DEMO not in sys.path:
    sys.path.insert(0, _DEMO)

import stt_extract  # noqa: E402  (existing extractor; called, never modified)

from .event_extractor import extract_events_for_capture  # noqa: E402
from .models import (  # noqa: E402
    AuditAction, AuditEntry, Capture, CaptureContext, CaptureSource, CaptureStatus, Event, Role,
    SourceRecord, now, require_aware,
)
from .store import IpdStore  # noqa: E402
from .timeline import ACTIVE_ENCOUNTER_STATUSES, Timeline  # noqa: E402

PIPELINE = "ipd.capture.typed_ward_round/v1"
EXTRACTOR_SOURCE_MODE = "image"  # see module docstring: safe typed-text mode of run_text_extract
SOURCE_ENGINE = "typed_text"
MAX_TEXT_CHARS = 20000
WARD_ROUND_ROLES = frozenset({Role.CONSULTANT, Role.RESIDENT, Role.INTENSIVIST})


class CaptureError(ValueError):
    """Rejected before anything was written."""


class CaptureProcessingError(RuntimeError):
    """Processing failed; the capture is stored as FAILED (see .result)."""

    def __init__(self, message: str, result: "CaptureResult"):
        super().__init__(message)
        self.result = result


@dataclass(frozen=True)
class CaptureResult:
    capture: Capture
    source_record: Optional[SourceRecord]
    entities: dict = field(default_factory=dict)       # run_text_extract entities_json
    transcript: dict = field(default_factory=dict)     # run_text_extract transcript_json
    events: Tuple[Event, ...] = ()                     # all events produced for this capture
    appended_event_ids: Tuple[str, ...] = ()           # newly written this call
    duplicate_event_ids: Tuple[str, ...] = ()          # already on the timeline (reprocessing)
    skipped: Tuple[dict, ...] = ()                     # extractor items not mapped (with reason)
    error: Optional[str] = None

    @property
    def status(self) -> CaptureStatus:
        return self.capture.status

    @property
    def ok(self) -> bool:
        return self.capture.status == CaptureStatus.PROCESSED and self.error is None


# ---------------------------------------------------------------- helpers
def _typed_segments(text: str) -> list:
    """One segment per non-empty line; typed text has no timing, so none is added."""
    return [{"id": i, "text": line} for i, line in enumerate(l for l in text.split("\n") if l.strip())]


def _check_encounter(store: IpdStore, encounter_id: str):
    enc = store.get_encounter(encounter_id)
    if enc is None:
        raise CaptureError(f"unknown encounter {encounter_id}")
    if enc.status not in ACTIVE_ENCOUNTER_STATUSES:
        raise CaptureError(f"encounter {encounter_id} is not active ({enc.status.value})")
    return enc


def _run_pipeline(capture: Capture, text: str):
    """Existing extractor (deterministic, no LLM) + IPD adapter. Pure: no DB writes."""
    tj, ej = stt_extract.run_text_extract(text, _typed_segments(text), job_id=capture.id, use_llm=False,
                                          engine=SOURCE_ENGINE, source=EXTRACTOR_SOURCE_MODE)
    source = SourceRecord(capture_id=capture.id, text=text, normalized_text=tj.get("normalized_en", ""),
                          segments=tj.get("segments", []),
                          engine=f"{SOURCE_ENGINE}; run_text_extract(source={EXTRACTOR_SOURCE_MODE}, use_llm=False)")
    events, skipped = extract_events_for_capture(ej, capture, source, recorded_at=now())
    return tj, ej, source, events, skipped


def _info(ej=None, events=(), appended=(), dups=(), skipped=(), error=None) -> dict:
    info = {"pipeline": PIPELINE, "extractor": "stt_extract.run_text_extract",
            "extractor_source_mode": EXTRACTOR_SOURCE_MODE, "use_llm": False,
            "adapter": "ipd.event_extractor", "llm_engine": (ej or {}).get("llm_engine", ""),
            "n_events": len(events), "n_appended": len(appended), "n_duplicates": len(dups),
            "n_skipped": len(skipped)}
    if error:
        info["error"] = error
    return info


def _audit(capture: Capture, detail: dict) -> AuditEntry:
    return AuditEntry(user_id=capture.author_id, role=capture.author_role, action=AuditAction.CREATE,
                      entity="capture", entity_id=capture.id, detail={"encounter_id": capture.encounter_id, **detail})


def _record_failure(store: IpdStore, capture: Capture, source: Optional[SourceRecord], error: str,
                    exists: bool) -> Capture:
    """Persist FAILED status (and the typed text if possible). Never removes anything."""
    info = {**capture.pipeline_info, **_info(error=error)}
    if exists:
        # A capture whose events are already on the timeline stays PROCESSED (its earlier events are
        # valid); the failed attempt is recorded, and the caller still gets an error.
        keep = capture.status == CaptureStatus.PROCESSED
        if keep:
            info = {**capture.pipeline_info, "last_reprocess_error": error}
        store.set_capture_status(capture.id, CaptureStatus.PROCESSED if keep else CaptureStatus.FAILED, info)
        store.append_audit(_audit(capture, {"status": "reprocess_failed", "operation": "reprocess", "error": error}))
        return store.get_capture(capture.id)
    failed = Capture(**{**capture.__dict__, "status": CaptureStatus.FAILED, "pipeline_info": info})
    with store.transaction():
        store.add_capture(failed)
        store.append_audit(_audit(failed, {"status": "failed", "error": error}))
    if source is not None:
        try:
            store.add_source_record(source)
        except Exception as e:  # keep the FAILED capture even if the text cannot be stored
            info["source_record_error"] = f"{type(e).__name__}: {e}"
            store.set_capture_status(failed.id, CaptureStatus.FAILED, info)
    return store.get_capture(failed.id)


# ---------------------------------------------------------------- public API
def capture_typed_ward_round(store: IpdStore, *, encounter_id: str, text: str, author_id: str,
                             author_role: Role, location: dict, captured_at: datetime,
                             capture_id: Optional[str] = None) -> CaptureResult:
    """Record one typed ward-round note and append its events to the timeline.

    Raises CaptureError (nothing written) for invalid input or an inactive encounter,
    CaptureProcessingError (capture stored as FAILED) if processing fails.
    Passing an existing capture_id reprocesses that capture instead (idempotent).
    """
    role = Role(author_role)
    if role not in WARD_ROUND_ROLES:
        raise CaptureError(f"role {role.value} cannot record a ward round")
    if not isinstance(author_id, str) or not author_id.strip():
        raise CaptureError("author_id is required")
    if not isinstance(text, str) or not text.strip():
        raise CaptureError("typed text is empty")
    if len(text) > MAX_TEXT_CHARS:
        raise CaptureError(f"typed text longer than {MAX_TEXT_CHARS} characters")
    if not isinstance(location, dict) or not all(isinstance(location.get(k), str) and location[k].strip()
                                                 for k in ("ward", "bed")):
        raise CaptureError("location must include ward and bed")
    try:
        require_aware(captured_at, "captured_at")
    except ValueError as e:
        raise CaptureError(str(e)) from None
    _check_encounter(store, encounter_id)

    if capture_id is not None and store.get_capture(capture_id) is not None:
        existing, src = store.get_capture(capture_id), store.get_source_record(capture_id)
        if existing.encounter_id != encounter_id or src is None or src.text != text:
            raise CaptureError(f"capture {capture_id} exists with different encounter or text")
        return reprocess_capture(store, capture_id)

    kw = {"id": capture_id} if capture_id else {}
    capture = Capture(encounter_id=encounter_id, source=CaptureSource.TEXT, capture_context=CaptureContext.WARD_ROUND,
                      author_id=author_id.strip(), author_role=role, captured_at=captured_at, location=dict(location),
                      status=CaptureStatus.RECEIVED, pipeline_info=_info(), **kw)
    source = SourceRecord(capture_id=capture.id, text=text, engine=SOURCE_ENGINE)  # replaced after extraction
    try:
        tj, ej, source, events, skipped = _run_pipeline(capture, text)
    except Exception as e:
        err = f"extraction failed: {type(e).__name__}: {e}"
        failed = _record_failure(store, capture, source, err, exists=False)
        raise CaptureProcessingError(err, CaptureResult(capture=failed, source_record=store.get_source_record(capture.id),
                                                        error=err)) from e
    timeline = Timeline(store)
    try:
        with store.transaction():
            done = Capture(**{**capture.__dict__, "status": CaptureStatus.PROCESSED})
            store.add_capture(done)
            store.add_source_record(source)
            res = timeline.append_many(encounter_id, events)
            info = _info(ej, events, res.appended, res.duplicates, skipped)
            store.set_capture_status(done.id, CaptureStatus.PROCESSED, info)
            store.append_audit(_audit(done, {"status": "processed", "n_appended": len(res.appended)}))
    except Exception as e:
        err = f"persisting capture/events failed: {type(e).__name__}: {e}"
        failed = _record_failure(store, capture, source, err, exists=False)
        raise CaptureProcessingError(err, CaptureResult(
            capture=failed, source_record=store.get_source_record(capture.id), entities=ej, transcript=tj,
            events=tuple(events), skipped=tuple(skipped), error=err)) from e
    return CaptureResult(capture=store.get_capture(capture.id), source_record=store.get_source_record(capture.id),
                         entities=ej, transcript=tj, events=tuple(events),
                         appended_event_ids=tuple(e.id for e in res.appended),
                         duplicate_event_ids=tuple(res.duplicates), skipped=tuple(skipped))


def reprocess_capture(store: IpdStore, capture_id: str) -> CaptureResult:
    """Re-run extraction for a stored typed ward-round capture. Appends only new events."""
    capture = store.get_capture(capture_id)
    if capture is None:
        raise CaptureError(f"unknown capture {capture_id}")
    if capture.source != CaptureSource.TEXT or capture.capture_context != CaptureContext.WARD_ROUND:
        raise CaptureError("only typed ward-round captures can be reprocessed here")
    stored = store.get_source_record(capture_id)
    if stored is None:
        raise CaptureError(f"capture {capture_id} has no stored text to reprocess")
    _check_encounter(store, capture.encounter_id)
    try:
        tj, ej, _, events, skipped = _run_pipeline(capture, stored.text)
        with store.transaction():
            res = Timeline(store).append_many(capture.encounter_id, events)
            store.set_capture_status(capture_id, CaptureStatus.PROCESSED,
                                     _info(ej, events, res.appended, res.duplicates, skipped))
            store.append_audit(_audit(capture, {"status": "processed", "operation": "reprocess",
                                                "n_appended": len(res.appended)}))
    except Exception as e:
        err = f"reprocessing failed: {type(e).__name__}: {e}"
        failed = _record_failure(store, capture, None, err, exists=True)
        raise CaptureProcessingError(err, CaptureResult(capture=failed, source_record=stored, error=err)) from e
    return CaptureResult(capture=store.get_capture(capture_id), source_record=stored, entities=ej, transcript=tj,
                         events=tuple(events), appended_event_ids=tuple(e.id for e in res.appended),
                         duplicate_event_ids=tuple(res.duplicates), skipped=tuple(skipped))
