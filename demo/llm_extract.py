"""LLM-primary clinical extraction (new.md Sec 5) with regex offline fallback.

Chain: STT transcript + template field schema -> local Ollama strict JSON
(chief_complaint, vitals, drugs, allergies, denied, diagnosis, followup,
each with proof source_sentence + seg id) -> hand-validated against
templates/er_discharge.schema.json rules -> entities_json shape.
Any failure (no binary/model, timeout, bad JSON, schema violation) raises
ValueError(reason) so callers fall back to regex extract_entities.
"""
import json
import os
import re
import shutil
import subprocess

_HERE = os.path.dirname(os.path.abspath(__file__))
UNITS_OK = {"mg", "mcg", "g", "ml", "U"}


def _regex_vitals_slots(regex_ent):
    """Which vitals slots regex already filled with digits (gap-fill gate for LLM)."""
    import re as _re
    has = {"bp": False, "temp": False, "spo2": False}
    for v in regex_ent.get("vitals", []) or []:
        t = str(v.get("text", ""))
        if _re.search(r"\d+\s*(?:by|/|over|of)\s*\d+", t):
            has["bp"] = True
        if _re.search(r"degree|temp|fahrenheit|°", t, _re.I):
            has["temp"] = True
        if _re.search(r"spo2|o2|%|oxygen|percent", t, _re.I):
            has["spo2"] = True
    return has


_SV_KIND = {"sys": "bp", "dia": "bp", "temp": "temp", "spo2": "spo2"}


def _llm_vitals_kind(item, llm_sv=None):
    """Classify an LLM vitals item into bp/temp/spo2/other for gap-fill gating.

    llm_sv: the same LLM output's structured_vitals; a unit-less item ("100.4",
    "99") that repeats one of its values takes that value's kind.
    """
    import re as _re
    t = str(item.get("text", item.get("name", "")))
    if _re.search(r"\d+\s*(?:by|/|over|of)\s*\d+|^BP\b", t, _re.I):
        return "bp"
    if _re.search(r"degree|temp|fahrenheit|°", t, _re.I):
        return "temp"
    if _re.search(r"spo2|o2|%|oxygen|percent", t, _re.I):
        return "spo2"
    for k, kind in _SV_KIND.items():
        if t.strip() and t.strip().lower() == str((llm_sv or {}).get(k, "") or "").strip().lower():
            return kind
    return "other"


def _temp_unit(s):
    s = str(s)
    if re.search(r"fahren|°\s*F\b|\bF\b", s, re.I):
        return "F"
    if re.search(r"cels|centi|°\s*C\b|\bC\b", s, re.I):
        return "C"
    return None


def _same_vital(field, regex_val, ai_val):
    """Same reading? First number must match; temp units must not contradict (F vs C)."""
    a = re.findall(r"\d+(?:\.\d+)?", str(regex_val))
    b = re.findall(r"\d+(?:\.\d+)?", str(ai_val))
    if not a or not b or float(a[0]) != float(b[0]):
        return False
    if field == "temp":
        ua, ub = _temp_unit(regex_val), _temp_unit(ai_val)
        if ua and ub and ua != ub:
            return False
    return True


def _gapfill_structured_vitals(merged, llm_sv, filled):
    """LLM structured vitals fill only slots regex left empty.

    A regex-filled slot is never overwritten. When the LLM reads a different
    value there, the disagreement is recorded in vitals_conflicts (regex kept,
    human must verify) instead of being dropped silently.
    Returns (gap_fill_sv, conflicts).
    """
    from coords import regex_vital_boxes, regex_vital_readings
    regex_vals = regex_vital_boxes(merged.get("vitals", []))
    readings = regex_vital_readings(merged.get("vitals", []))
    sv = {}
    conflicts = [c for c in merged.get("vitals_conflicts", []) or [] if isinstance(c, dict)]
    seen = {c.get("field") for c in conflicts}
    for field, kind in _SV_KIND.items():
        ai = str(llm_sv.get(field, "") or "").strip()
        if not ai:
            continue
        if not filled.get(kind):
            sv[field] = llm_sv[field]
            continue
        # several regex readings: AI agreeing with any one of them is no conflict
        rvs = [r["values"][field] for r in readings.get(kind, []) if r["values"].get(field)] \
            or ([regex_vals[field]] if regex_vals.get(field) else [])
        if rvs and field not in seen and not any(_same_vital(field, rv, ai) for rv in rvs):
            conflicts.append({"field": field, "regex": " | ".join(rvs), "ai": ai,
                              "resolution": "regex kept - verify"})
            seen.add(field)
    return sv, conflicts


def mark_ai_field(item, field):
    """Provenance: `field` of this entity was filled by AI -> clinician must review it."""
    fields = list(item.get("ai_fields") or [])
    if field not in fields:
        fields.append(field)
    item["ai_fields"] = fields


def mark_ai_item(x):
    """Copy of an AI-provided entity flagged ai_suggested (strings become text items)."""
    if isinstance(x, dict):
        return {**x, "ai_suggested": True}
    return {"text": str(x), "negated": False, "color": "YELLOW", "ai_suggested": True}


def merge_primary(regex_ent, llm_ent, model="llama3.2:3b"):
    """LLM gap-fills over regex base; regex numerals always win conflicts."""
    import difflib
    merged = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
              for k, v in regex_ent.items()}
    merged["drugs"] = [dict(d) for d in merged.get("drugs", []) or []]  # never mutate the regex base
    names = [(str(d.get("name", "")).lower(), d) for d in merged.get("drugs", [])]
    for t in llm_ent.get("drugs", []):
        tn = str(t.get("name", "")).lower()
        best, score = None, 0
        for n, d in names:
            s = difflib.SequenceMatcher(None, tn, n).ratio()
            if s > score:
                best, score = d, s
        if best is not None and score >= 0.6:
            for k in ("dose", "unit", "frequency", "duration"):
                if best.get(k) in (None, "", 0) and t.get(k) not in (None, "", 0):
                    best[k] = t[k]
                    mark_ai_field(best, k)  # regex row stays; this field is AI
        elif tn:
            t = mark_ai_item(t)
            t["note"] = (t.get("note", "") + " llm-primary").strip()
            merged["drugs"].append(t)
            names.append((tn, merged["drugs"][-1]))
    for k in ("diagnosis", "followup"):
        if not merged.get(k) and llm_ent.get(k):
            merged[k] = mark_ai_item(llm_ent[k])
    filled = _regex_vitals_slots(merged)
    for k in ("allergies", "symptoms"):
        # allergies keyed with negation: an AI "allergic to X" next to a regex
        # "denies X" is kept (shown as a conflict), never dropped as a duplicate
        key = (lambda x: (str(x.get("text", x.get("name", ""))).lower(), bool(x.get("negated")))) \
            if k == "allergies" else (lambda x: str(x.get("text", x.get("name", ""))).lower())
        seen = {key(x) for x in merged.get(k, [])}
        for x in llm_ent.get(k, []):
            if key(x) not in seen:
                merged[k].append(mark_ai_item(x))
    # Vitals: gap-fill only — never overwrite a regex-filled slot with LLM digits.
    llm_sv = dict(llm_ent.get("structured_vitals", {}) or {})
    seen_v = {str(x.get("text", x.get("name", ""))).lower() for x in merged.get("vitals", [])}
    for x in llm_ent.get("vitals", []) or []:
        kind = _llm_vitals_kind(x, llm_sv)
        if kind in filled and filled[kind]:
            continue
        if str(x.get("text", x.get("name", ""))).lower() in seen_v:
            continue
        merged["vitals"].append(mark_ai_item(x))
    # Structured vitals: only fill slots regex left empty; disagreements recorded.
    sv, conflicts = _gapfill_structured_vitals(merged, llm_sv, filled)
    if sv:
        merged["structured_vitals"] = sv
        merged["structured_vitals_ai"] = sorted(sv)  # every gap-filled box is AI
    if conflicts:
        merged["vitals_conflicts"] = conflicts
    merged["llm_engine"] = f"regex+llm-primary:{model}"
    return merged


def ollama_available(model="llama3.2:3b"):
    if not shutil.which("ollama"):
        return False, "no ollama binary"
    try:
        out = subprocess.run(["ollama", "list"], capture_output=True,
                             encoding="utf-8", errors="ignore", timeout=15)
        if model in (out.stdout or ""):
            return True, ""
        return False, f"model {model} not pulled"
    except Exception as e:
        return False, type(e).__name__


def _prompt(transcript_text, normalized_en):
    return (
        "You are a clinical extractor for ER discharge notes.\n"
        "Input may mix Hindi/Tamil/English.\n\n"
        "Return STRICT JSON only (no prose, no markdown).\n"
        "Schema:\n"
        "{\n"
        '  "chief_complaint": "verbatim symptom text",\n'
        '  "history": "normalized English history",\n'
        '  "drugs": [{"name":"...", "dose":500, "unit":"mg", "frequency":"twice daily",\n'
        '             "duration":"3 days", "source_sentence":"exact sentence heard",\n'
        '             "seg_id": 0}],\n'
        '  "vitals": {"sys":"", "dia":"", "temp":"", "spo2":""},\n'
        '  "allergies_active": [],\n'
        '  "allergies_denied": [],\n'
        '  "symptoms_denied": [],\n'
        '  "diagnosis": {"text":""},\n'
        '  "followup": {"text":""}\n'
        "}\n\n"
        "Rules (anti-hallucination, must follow):\n"
        "- Output ONLY values heard in the transcript. NEVER invent BP, temp, SpO2, drugs, or diagnoses.\n"
        "- If a field was not heard, use empty string or empty array (NOT null, NOT example values).\n"
        "- Doses numeric + UCUM units (mg/mcg/g/ml/U).\n"
        "- source_sentence MUST be a verbatim substring of the transcript; chief_complaint likewise.\n"
        "- Vitals: sys/dia digits only (with decimals if heard), temp with decimals + °F/°C/degrees as heard, spo2 with %.\n"
        "- seg_id = segment index where the evidence sentence occurs; must be within the transcript.\n\n"
        f"Transcript:\n{transcript_text[:2000]}\n\n"
        f"Normalized:\n{normalized_en[:2000]}"
    )


def _valid_drug(d):
    if not isinstance(d, dict) or not d.get("name"):
        return None
    try:
        dose = float(d["dose"]) if d.get("dose") not in (None, "") else None
    except (TypeError, ValueError):
        dose = None
    unit = d.get("unit")
    unit = unit if unit in UNITS_OK else None
    missing_numeral = dose is None or unit is None
    return {"name": str(d["name"]), "dose": dose,
            "unit": unit,
            "frequency": d.get("frequency", "") or "",
            "duration": d.get("duration", "") or "",
            "confidence": 0.70 if missing_numeral else 0.9,
            "color": "RED" if missing_numeral else "YELLOW",
            "source_sentence": d.get("source_sentence", ""), "negated": False,
            "note": "llm-primary" + (" dose/unit missing" if missing_numeral else "")}


def _extract_json_from_output(output):
    """Extract the last complete JSON object from LLM output, handling streaming artifacts."""
    if not output:
        return None
    # Remove markdown code fences if present
    output = re.sub(r'```json\s*', '', output)
    output = re.sub(r'```\s*$', '', output)
    # Remove ANSI escape sequences
    output = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', output)
    output = re.sub(r'\u001b\[[0-9;]*[A-Za-z]', '', output)
    # Remove other control characters
    output = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', output)
    # Find all JSON-like objects and return the last complete one
    # Use a stack-based approach to find balanced braces
    brace_stack = []
    json_candidates = []
    for i, ch in enumerate(output):
        if ch == '{':
            brace_stack.append(i)
        elif ch == '}' and brace_stack:
            start = brace_stack.pop()
            if not brace_stack:  # Complete object
                json_candidates.append(output[start:i+1])
    # Return the last (most complete) candidate
    return json_candidates[-1] if json_candidates else None


def extract_llm_primary(transcript_text, normalized_en, segments,
                        model="llama3.2:3b", timeout=60):
    ok, reason = ollama_available(model)
    if not ok:
        raise ValueError(reason)
    # Retry up to 3 times with exponential backoff
    last_error = None
    for attempt in range(3):
        try:
            out = subprocess.run(["ollama", "run", "--format", "json", model, _prompt(transcript_text, normalized_en)],
                                 capture_output=True, encoding="utf-8", errors="ignore",
                                 timeout=timeout)
        except Exception as e:
            last_error = ValueError(f"ollama run failed: {type(e).__name__}")
            continue
        json_str = _extract_json_from_output(out.stdout or "")
        if not json_str:
            last_error = ValueError("no JSON in LLM output")
            continue
        try:
            j = json.loads(json_str)
        except Exception as e:
            last_error = ValueError(f"LLM JSON unparsable: {e}")
            continue
        if not isinstance(j, dict) or not any(k in j for k in ("drugs", "chief_complaint", "vitals")):
            last_error = ValueError("LLM JSON missing required keys")
            continue
        # Proof gate: source_sentence must be a verbatim transcript substring,
        # else clear it and cap confidence (anti-hallucination).
        _full = f"{transcript_text} {normalized_en}"
        def _gate(item):
            s = str(item.get("source_sentence", "") or "")
            if s and s[:40] not in _full and s not in _full:
                item = dict(item)
                item["source_sentence"] = ""
                item["confidence"] = min(float(item.get("confidence", 0.9)), 0.80)
                item["note"] = (str(item.get("note", "")) + " unprovenance").strip()
            return item
        # Process the parsed JSON into entities format
        drugs = [d for d in (_gate(_valid_drug(x)) if _valid_drug(x) else None
                             for x in j.get("drugs", []) or []) if d]
        v = j.get("vitals", {}) if isinstance(j.get("vitals"), dict) else {}
        vitals = []
        structured_vitals = {}
        import re as _re2
        # Digit-presence gate: LLM vitals digits must occur in transcript/normalized text.
        def _digits(s):
            return _re2.findall(r"\d+(?:\.\d+)?", str(s))
        if v.get("sys") and v.get("dia"):
            # exact reading with a BP label in the source; digits inside a
            # date ("05/05/2023") or elsewhere never count
            from coords import bp_supported
            if bp_supported(v["sys"], v["dia"], _full):
                vitals.append({"text": f"BP {v['sys']}/{v['dia']}", "confidence": 0.88,
                               "color": "YELLOW", "source_sentence": ""})
                structured_vitals["sys"] = str(v["sys"])
                structured_vitals["dia"] = str(v["dia"])
        for k in ("temp", "spo2"):
            if v.get(k):
                dk = _digits(v[k])
                if dk and dk[0] in _full:
                    vitals.append({"text": str(v[k]), "confidence": 0.87, "color": "YELLOW",
                                   "source_sentence": ""})
                    structured_vitals[k] = str(v[k])
        allergies = [{"text": str(a), "negated": True, "confidence": 0.9,
                      "source_sentence": "", "note": "NEGATED - llm-primary"}
                     for a in (j.get("allergies_denied", []) or [])]
        # documented allergies: kept only if the allergen is spoken in the
        # transcript (never invented), YELLOW = clinician verifies
        allergies += [{"text": str(a), "negated": False, "confidence": 0.82, "color": "YELLOW",
                       "source_sentence": "", "note": "llm-primary - verify"}
                      for a in (j.get("allergies_active", []) or [])
                      if str(a).strip() and str(a).strip().lower() in _full.lower()]
        symptoms = ([{"text": str(j["chief_complaint"]), "confidence": 0.9, "color": "YELLOW",
                      "source_sentence": "", "negated": False}] if j.get("chief_complaint") else [])
        symptoms += [{"text": str(s), "confidence": 0.91, "color": "YELLOW",  # AI denial: verify
                      "source_sentence": "", "negated": True, "note": "DENIED - excluded"}
                     for s in (j.get("symptoms_denied", []) or [])]
        dx = j.get("diagnosis", {}) if isinstance(j.get("diagnosis"), dict) else {}
        result = {"drugs": drugs, "symptoms": symptoms, "vitals": vitals,
                  "structured_vitals": structured_vitals,
                  "allergies": allergies,
                  "negations": [{"span": str(a), "negated": True} for a in (j.get("allergies_denied", []) or [])],
                  "diagnosis": {"text": str(dx.get("text", "")), "icd10": "",
                                "confidence": 0.85, "color": "YELLOW", "source_sentence": ""}
                  if dx.get("text") else {},
                  "followup": {"text": str(j.get("followup", {}).get("text", "") if isinstance(j.get("followup"), dict) else j.get("followup", "")),
                               "confidence": 0.88, "color": "YELLOW", "source_sentence": ""}
                  if j.get("followup") else {},
                  "llm_engine": f"ollama-primary:{model}"}
        return result
    raise last_error or ValueError("LLM extraction failed after retries")
