"""IPD P1 step 3: entities_json (existing extractor output) -> timeline Events.

Pure, deterministic adapter. It never calls an LLM, never writes to SQLite,
and never adds clinical content that the extractor did not produce.

Mappings
  symptoms[]            -> SYMPTOM/present | SYMPTOM/denied (negation kept)
  vitals[], structured_vitals -> VITAL/bp | temperature | spo2 | pulse
  diagnosis{}           -> DIAGNOSIS/provisional
  drugs[]               -> MEDICATION_ORDER/proposed (always UNVERIFIED)
  allergies[]           -> ALLERGY/active | ALLERGY/denied
  followup{}            -> PLAN/followup
Ignored (not clinical facts or derived): negations, patient, ai_eval,
llm_engine, tidy_engine, job_id and any unknown key.

Provenance: source_sentence is kept in the payload. It is located in the
source text (exact match first, then case/whitespace-insensitive) to give
char_start/char_end and the overlapping segment(s). If it cannot be located,
no span is invented: source_span stays empty and payload.evidence_located is
False. Segment timing (start_ms/end_ms) is kept only when the segment has
real timing (end > start); document lines give ocr_line instead.

Event ids are derived from (source_capture_id, category, subtype, payload),
so the same capture + entities always yields the same ids, and re-running
extraction cannot insert duplicates (events.id is UNIQUE).
"""
import hashlib
import json
import re
from datetime import datetime
from typing import List, Optional, Tuple

from .models import (
    Capture, CaptureSource, Event, EventCategory, Role, SourceRecord, SourceType, Verification, now,
)

# Used only when an extractor output carries no confidence (e.g. structured_vitals).
DEFAULT_CONFIDENCE = 0.5

# Exact-term synonyms the existing extractor emits for one mention (Hinglish);
# the same fixed pairs the ER template dedups. No fuzzy matching.
CANONICAL_TERMS = {"bukhar": "fever", "khansi": "cough"}

_SOURCE_FOR_CAPTURE = {CaptureSource.AUDIO: SourceType.SPOKEN, CaptureSource.TEXT: SourceType.TYPED,
                       CaptureSource.STRUCTURED_FORM: SourceType.TYPED,
                       CaptureSource.DOCUMENT_IMAGE: SourceType.DOCUMENT,
                       CaptureSource.DEVICE: SourceType.DEVICE, CaptureSource.HIS: SourceType.HIS}

_NUM = r"(\d{2,3}(?:\.\d{1,2})?)"
_BP_RE = re.compile(_NUM + r"\s*(?:/|by|over|of)\s*" + _NUM, re.I)
_TEMP_HINT = re.compile(r"degree|temp|fahrenheit|celsius|°", re.I)
_TEMP_RE = re.compile(_NUM + r"\s*(?:°\s*|degrees?\s*)?(fahrenheit|celsius|F|C)?\b", re.I)
_SPO2_HINT = re.compile(r"spo2|\bo2\b|oxygen|%|percent", re.I)
_PULSE_RE = re.compile(r"\b(?:pulse|heart\s*rate|hr)\b\D{0,10}(\d{2,3})", re.I)


# ---------------------------------------------------------------- helpers
def _num(s: str):
    return int(s) if re.fullmatch(r"\d+", s) else float(s)


def _confidence(item) -> Tuple[Optional[float], Optional[str]]:
    """(value, error). Missing -> DEFAULT_CONFIDENCE; invalid -> error."""
    c = item.get("confidence") if isinstance(item, dict) else None
    if c is None:
        return DEFAULT_CONFIDENCE, None
    if isinstance(c, bool) or not isinstance(c, (int, float)) or c != c or not 0.0 <= float(c) <= 1.0:
        return None, f"invalid confidence {c!r}"
    return float(c), None


def _text(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _norm_with_map(s: str):
    """Casefolded, whitespace-collapsed copy + index map back into s."""
    out, idx, prev_space = [], [], False
    for i, ch in enumerate(s):
        if ch.isspace():
            if prev_space:
                continue
            ch, prev_space = " ", True
        else:
            prev_space = False
        for c in ch.casefold():
            out.append(c)
            idx.append(i)
    return "".join(out), idx


def _locate(sentence: str, text: str) -> Optional[Tuple[int, int, bool]]:
    """(char_start, char_end, ambiguous) of sentence in text, or None."""
    if not sentence or not text:
        return None
    i = text.find(sentence)
    if i >= 0:
        return i, i + len(sentence), text.find(sentence, i + 1) >= 0
    ns, _ = _norm_with_map(sentence.strip())
    nt, idx = _norm_with_map(text)
    j = nt.find(ns) if ns else -1
    if j < 0:
        return None
    return idx[j], idx[j + len(ns) - 1] + 1, nt.find(ns, j + 1) >= 0


def _segment_offsets(segments, text):
    """[(seg, start, end)] for segments found in order within text."""
    out, cursor = [], 0
    for seg in segments or []:
        if not isinstance(seg, dict) or not isinstance(seg.get("text"), str) or not seg["text"]:
            continue
        i = text.find(seg["text"], cursor)
        if i < 0:
            continue
        out.append((seg, i, i + len(seg["text"])))
        cursor = i + len(seg["text"])
    return out


def _span(sentence, text, seg_offsets, source_type) -> Tuple[dict, dict]:
    """(source_span, provenance payload fields)."""
    prov = {"source_sentence": sentence, "evidence_located": False}
    loc = _locate(sentence, text)
    if loc is None:
        return {}, prov
    start, end, ambiguous = loc
    span = {"char_start": start, "char_end": end}
    prov["evidence_located"] = True
    if ambiguous:
        prov["evidence_ambiguous"] = True  # first occurrence used
    hits = [(s, a, b) for s, a, b in seg_offsets if a < end and start < b]
    if hits:
        first, last = hits[0][0], hits[-1][0]
        span["segment_id"] = first.get("id")
        if len(hits) > 1:
            span["segment_ids"] = [h[0].get("id") for h in hits]
        if source_type == SourceType.DOCUMENT:
            span["ocr_line"] = first.get("id")
        try:
            s0, e1 = float(first.get("start")), float(last.get("end"))
            if e1 > s0 >= 0:
                span["start_ms"], span["end_ms"] = int(round(s0 * 1000)), int(round(e1 * 1000))
        except (TypeError, ValueError):
            pass
    return span, prov


def parse_vital(text: str) -> Optional[Tuple[str, dict]]:
    """Deterministic parse of one extracted vital string -> (subtype, values)."""
    t = _text(text)
    if not t:
        return None
    m = _BP_RE.search(t)
    if m and not _TEMP_HINT.search(t):
        return "bp", {"systolic": _num(m.group(1)), "diastolic": _num(m.group(2))}
    m = _PULSE_RE.search(t)
    if m:
        return "pulse", {"value": _num(m.group(1))}
    if _TEMP_HINT.search(t) or re.search(r"\d\s*[FC]\b", t):
        m = _TEMP_RE.search(t)
        if m:
            u = (m.group(2) or "").lower()
            unit = "F" if u in ("f", "fahrenheit") else "C" if u in ("c", "celsius") else None
            return "temperature", {"value": _num(m.group(1)), "unit": unit}
    if _SPO2_HINT.search(t):
        m = re.search(_NUM, t)
        if m:
            return "spo2", {"value": _num(m.group(1)), "unit": "%"}
    return None


def _event_id(capture_id, category, subtype, payload) -> str:
    key = json.dumps([capture_id, category.value, subtype, payload], sort_keys=True, ensure_ascii=False)
    return "evt_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------- adapter
def extract_events_with_report(entities_json: dict, *, encounter_id: str, source_capture_id: str,
                               occurred_at: datetime, author_id: str, author_role: Role,
                               source_type: SourceType, source_text: str = "", segments: Optional[list] = None,
                               recorded_at: Optional[datetime] = None) -> Tuple[List[Event], List[dict]]:
    """Return (events, skipped). skipped = [{"field", "reason"}] for items not mapped."""
    if not isinstance(entities_json, dict):
        raise ValueError("entities_json must be a dict")
    if not source_capture_id:
        raise ValueError("source_capture_id is required (provenance)")
    source_type = SourceType(source_type)
    author_role = Role(author_role)
    recorded_at = recorded_at if recorded_at is not None else now()
    text = source_text if isinstance(source_text, str) else ""
    seg_offsets = _segment_offsets(segments, text)
    events, fields, skipped, seen = [], [], [], {}

    def add(field, category, subtype, payload, item, dedupe_key=None):
        conf, err = _confidence(item)
        if err:
            skipped.append({"field": field, "reason": err})
            return
        sentence = _text(item.get("source_sentence")) if isinstance(item, dict) else ""
        span, prov = _span(sentence, text, seg_offsets, source_type) if sentence else ({}, {"evidence_located": False})
        if not isinstance(item, dict) or "confidence" not in item:
            prov["confidence_source"] = "default"
        note = _text(item.get("note")) if isinstance(item, dict) else ""
        if note:
            prov["extractor_note"] = note
        full = {**payload, **prov}
        ev_id = _event_id(source_capture_id, category, subtype, full)
        key = dedupe_key if dedupe_key is not None else ("id", ev_id)
        prev = seen.get(key)
        if prev is not None:
            # same fact twice: keep the one with located evidence, else the first
            if events[prev].payload.get("evidence_located") or not full.get("evidence_located"):
                skipped.append({"field": field, "reason": f"duplicate of {fields[prev]}"})
                return
            skipped.append({"field": fields[prev], "reason": f"duplicate of {field}"})
        ev = Event(id=ev_id, encounter_id=encounter_id, occurred_at=occurred_at, recorded_at=recorded_at,
                   category=category, subtype=subtype, payload=full, confidence=conf,
                   verification=Verification.UNVERIFIED, author_id=author_id, author_role=author_role,
                   source_type=source_type, source_capture_id=source_capture_id, source_span=span)
        if prev is not None:
            events[prev], fields[prev] = ev, field  # replace in place: order stays deterministic
        else:
            seen[key] = len(events)
            events.append(ev)
            fields.append(field)

    def items(key):
        v = entities_json.get(key)
        if v is None:
            return []
        if not isinstance(v, list):
            skipped.append({"field": key, "reason": "expected a list"})
            return []
        return list(enumerate(v))

    # symptoms
    for i, s in items("symptoms"):
        f = f"symptoms[{i}]"
        term = _text(s.get("text")) if isinstance(s, dict) else ""
        if not term:
            skipped.append({"field": f, "reason": "missing symptom text"})
            continue
        canon = CANONICAL_TERMS.get(term.lower(), term.lower())
        denied = s.get("negated") is True
        add(f, EventCategory.SYMPTOM, "denied" if denied else "present",
            {"term": canon, "text": term, "negated": denied}, s, ("symptom", canon, denied))

    # vitals (free-text entries first, then structured_vitals)
    for i, v in items("vitals"):
        f = f"vitals[{i}]"
        raw = _text(v.get("text")) if isinstance(v, dict) else ""
        parsed = parse_vital(raw)
        if parsed is None:
            skipped.append({"field": f, "reason": f"unrecognised vital {raw!r}"})
            continue
        sub, vals = parsed
        add(f, EventCategory.VITAL, sub, {**vals, "text": raw}, v, ("vital", sub, json.dumps(vals, sort_keys=True)))
    sv = entities_json.get("structured_vitals")
    if sv is not None and not isinstance(sv, dict):
        skipped.append({"field": "structured_vitals", "reason": "expected an object"})
        sv = None
    if sv:
        sys_, dia = _text(str(sv.get("sys", "") or "")), _text(str(sv.get("dia", "") or ""))
        cands = []
        if sys_ or dia:
            cands.append(("structured_vitals.bp", f"{sys_}/{dia}" if sys_ and dia else ""))
        for k, label in (("temp", "temperature"), ("spo2", "spo2")):
            val = _text(str(sv.get(k, "") or ""))
            if val:
                hint = val if k == "temp" and _TEMP_HINT.search(val) else (f"temp {val}" if k == "temp" else f"SpO2 {val}")
                cands.append((f"structured_vitals.{k}", hint))
        for f, raw in cands:
            parsed = parse_vital(raw) if raw else None
            if parsed is None:
                skipped.append({"field": f, "reason": "incomplete or unrecognised structured vital"})
                continue
            sub, vals = parsed
            add(f, EventCategory.VITAL, sub, {**vals, "text": raw, "structured": True}, {},
                ("vital", sub, json.dumps(vals, sort_keys=True)))

    # diagnosis
    dx = entities_json.get("diagnosis")
    if isinstance(dx, dict) and _text(dx.get("text")):
        p = {"text": _text(dx["text"])}
        if _text(dx.get("icd10")):
            p["icd10"] = _text(dx["icd10"])
        add("diagnosis", EventCategory.DIAGNOSIS, "provisional", p, dx)
    elif dx not in (None, {}) and not isinstance(dx, dict):
        skipped.append({"field": "diagnosis", "reason": "expected an object"})

    # drugs -> proposed orders only
    for i, d in items("drugs"):
        f = f"drugs[{i}]"
        name = _text(d.get("name")) if isinstance(d, dict) else ""
        if not name:
            skipped.append({"field": f, "reason": "missing drug name"})
            continue
        if d.get("negated") is True:
            skipped.append({"field": f, "reason": "negated drug mention is not an order"})
            continue
        p, missing = {"name": name}, []
        dose = d.get("dose")
        if isinstance(dose, (int, float)) and not isinstance(dose, bool) and dose == dose and dose > 0:
            p["dose"] = dose
        else:
            missing.append("dose")
        for k in ("unit", "frequency", "duration"):
            if _text(d.get(k)):
                p[k] = _text(d[k])
            else:
                missing.append(k)
        if missing:
            p["missing"] = missing
        add(f, EventCategory.MEDICATION_ORDER, "proposed", p, d)

    # allergies
    for i, a in items("allergies"):
        f = f"allergies[{i}]"
        sub = _text(a.get("text")) if isinstance(a, dict) else ""
        if not sub:
            skipped.append({"field": f, "reason": "missing allergy substance"})
            continue
        denied = a.get("negated") is True
        add(f, EventCategory.ALLERGY, "denied" if denied else "active",
            {"substance": sub, "negated": denied}, a, ("allergy", sub.lower(), denied))

    # follow-up
    fu = entities_json.get("followup")
    if isinstance(fu, dict) and _text(fu.get("text")):
        add("followup", EventCategory.PLAN, "followup", {"text": _text(fu["text"])}, fu)
    elif fu not in (None, {}) and not isinstance(fu, dict):
        skipped.append({"field": "followup", "reason": "expected an object"})

    return events, skipped


def extract_events(entities_json: dict, **kwargs) -> List[Event]:
    """Events only (see extract_events_with_report for skipped items)."""
    return extract_events_with_report(entities_json, **kwargs)[0]


def extract_events_for_capture(entities_json: dict, capture: Capture, source: Optional[SourceRecord] = None,
                               occurred_at: Optional[datetime] = None,
                               recorded_at: Optional[datetime] = None) -> Tuple[List[Event], List[dict]]:
    """Convenience: take encounter/author/time/source type from a Capture."""
    return extract_events_with_report(
        entities_json, encounter_id=capture.encounter_id, source_capture_id=capture.id,
        occurred_at=occurred_at or capture.captured_at, author_id=capture.author_id,
        author_role=capture.author_role, source_type=_SOURCE_FOR_CAPTURE[capture.source],
        source_text=source.text if source else "", segments=source.segments if source else None,
        recorded_at=recorded_at)
