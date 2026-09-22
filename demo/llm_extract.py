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
        if _re.search(r"spo2|o2|%|oxygen", t, _re.I):
            has["spo2"] = True
    return has


def _llm_vitals_kind(item):
    """Classify an LLM vitals item into bp/temp/spo2/other for gap-fill gating."""
    import re as _re
    t = str(item.get("text", item.get("name", "")))
    if _re.search(r"\d+\s*(?:by|/|over|of)\s*\d+|^BP\b", t, _re.I):
        return "bp"
    if _re.search(r"degree|temp|fahrenheit|°", t, _re.I):
        return "temp"
    if _re.search(r"spo2|o2|%|oxygen", t, _re.I):
        return "spo2"
    return "other"


def merge_primary(regex_ent, llm_ent, model="llama3.2:3b"):
    """LLM gap-fills over regex base; regex numerals always win conflicts."""
    import difflib
    merged = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
              for k, v in regex_ent.items()}
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
        elif tn:
            t = dict(t)
            t["note"] = (t.get("note", "") + " llm-primary").strip()
            merged["drugs"].append(t)
            names.append((tn, merged["drugs"][-1]))
    for k in ("diagnosis", "followup"):
        if not merged.get(k) and llm_ent.get(k):
            merged[k] = llm_ent[k]
    filled = _regex_vitals_slots(merged)
    for k in ("allergies", "symptoms"):
        seen = {str(x.get("text", x.get("name", ""))).lower() for x in merged.get(k, [])}
        for x in llm_ent.get(k, []):
            if str(x.get("text", x.get("name", ""))).lower() not in seen:
                merged[k].append(x)
    # Vitals: gap-fill only — never overwrite a regex-filled slot with LLM digits.
    seen_v = {str(x.get("text", x.get("name", ""))).lower() for x in merged.get("vitals", [])}
    for x in llm_ent.get("vitals", []) or []:
        kind = _llm_vitals_kind(x)
        if kind in filled and filled[kind]:
            continue
        if str(x.get("text", x.get("name", ""))).lower() in seen_v:
            continue
        merged["vitals"].append(x)
    # Structured vitals: only fill slots regex left empty.
    sv = dict(llm_ent.get("structured_vitals", {}) or {})
    if sv:
        if filled["bp"]:
            sv.pop("sys", None)
            sv.pop("dia", None)
        if filled["temp"]:
            sv.pop("temp", None)
        if filled["spo2"]:
            sv.pop("spo2", None)
        if sv:
            merged["structured_vitals"] = sv
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
            ds, dd = _digits(v["sys"]), _digits(v["dia"])
            if ds and dd and ds[0] in _full and dd[0] in _full:
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
        symptoms = ([{"text": str(j["chief_complaint"]), "confidence": 0.9, "color": "YELLOW",
                      "source_sentence": "", "negated": False}] if j.get("chief_complaint") else [])
        symptoms += [{"text": str(s), "confidence": 0.91, "color": "GREEN",
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
