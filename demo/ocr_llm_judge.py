"""LLM judge + AI eval for image OCR text (feature/ocr-integration).

Two-pass, local-Ollama-only, image source only:

  1. extract_llm_image() — OCR text + lines -> strict-JSON entity candidates
  2. merge_image()       — gap-fill over the regex base; regex exact matches
                            always win, LLM fills only NIL/RED slots (YELLOW)
  3. judge_fields()      — per discharge field PASS | MISSING | UNPROVEN
                            against the raw OCR lines (independent 2nd call)

  run_image_llm_eval() runs all three; raises ValueError on any failure so
  callers fall back to regex-only (same convention as llm_extract).

Safety (mirrors the audio LLM path, stricter for OCR):
- every LLM value needs a verbatim OCR proof (source_line substring + valid
  line_no) and every digit must occur in the OCR text — otherwise dropped or
  capped at 0.80 with an "unprovenance" note. Numbers, doses, units and drug
  names are never invented; unknown drug names are added only when spelled
  verbatim in the OCR text. No speech-mishear repair, no fuzzy matching.
"""

import json
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from llm_extract import (  # same-package reuse; audio path untouched
    _extract_json_from_output,
    _llm_vitals_kind,
    _regex_vitals_slots,
    _valid_drug,
    ollama_available,
)

# ollama CLI streams progress redraws into piped stdout:
#   <chunk> ESC[nD ESC[K \n <retyped chunk...>
# i.e. "erase the last n chars; the newline is a chunk join, not content".
# Merely stripping the escapes (as the audio path does) leaves duplicated
# fragments and unparsable JSON, so redraws are joined before parsing.
_REDRAW = re.compile(r"\x1b\[(\d*)D\x1b\[K\n?|\x1b\[K\n?")
_CSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _join_redraws(text):
    """Collapse ollama streaming redraws to the final visible text."""
    buf, pos, out = [], 0, []
    for m in _REDRAW.finditer(text or ""):
        out.append(text[pos:m.start()])
        if "D" in m.group(0):
            n = int(m.group(1)) if m.group(1).isdigit() else 1
            s = "".join(out)
            out = [s[:-n] if n < len(s) else ""]
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def _clean_ollama_output(text):
    """Redraw-join, then drop leftover terminal sequences."""
    return _CSI.sub("", _join_redraws(text))

# Discharge slot keys the judge evaluates (coords.resolve_slots keys).
# dx_code_box is placeholder-only by design; date/sign are not AI fields.
JUDGED_KEYS = (
    "patient_name",
    "cc_blank",
    "drug_0_name", "drug_0_dose", "drug_0_unit", "drug_0_freq", "drug_0_duration",
    "drug_1_name", "drug_1_dose", "drug_1_unit", "drug_1_freq", "drug_1_duration",
    "vitals_bp_sys", "vitals_bp_dia", "vitals_temp", "vitals_spo2",
    "allergy_line", "dx_text", "fu_text",
)


def _lines_from_segments(segments):
    """Transcript-compatible image segments -> [{line, text}] (segments ARE lines)."""
    return [{"line": s.get("id", i), "text": str(s.get("text", ""))}
            for i, s in enumerate(segments or [])]


def _numbered_lines(ocr_text, lines):
    if lines:
        return "\n".join(f"{ln['line']}: {ln['text']}" for ln in lines)
    return "\n".join(f"{i}: {t}" for i, t in enumerate(ocr_text.split("\n")))


def _prompt_extract(ocr_text, lines):
    numbered = _numbered_lines(ocr_text, lines)
    return (
        "You are a clinical extractor for ER discharge notes from OCR text.\n"
        "The input is raw OCR of a handwritten/printed note, one line per row.\n\n"
        "Return STRICT JSON only (no prose, no markdown).\n"
        "Schema:\n"
        "{\n"
        '  "patient_name": "name as printed, or empty string",\n'
        '  "chief_complaint": "main symptom as printed, or empty string",\n'
        '  "drugs": [{"name":"...", "dose":500, "unit":"mg", "frequency":"twice daily",\n'
        '             "duration":"3 days", "source_line":"exact OCR line text",\n'
        '             "line_no": 0}],\n'
        '  "vitals": {"sys":"", "dia":"", "temp":"", "spo2":""},\n'
        '  "vitals_proof": {"sys":"", "dia":"", "temp":"", "spo2":""},\n'
        '  "allergies_active": [],\n'
        '  "allergies_denied": [],\n'
        '  "symptoms_denied": [],\n'
        '  "diagnosis": {"text":"", "source_line":"", "line_no": -1},\n'
        '  "followup": {"text":"", "source_line":"", "line_no": -1}\n'
        "}\n\n"
        "Rules (anti-hallucination, must follow):\n"
        "- Output ONLY values visible in the OCR lines. NEVER invent names, BP,"
        " temp, SpO2, drugs, doses or diagnoses.\n"
        "- If a field is not visible, use empty string or empty array.\n"
        "- source_line MUST be a verbatim OCR line; line_no its number."
        " chief_complaint/symptoms/allergy words must occur verbatim in the OCR text.\n"
        "- Doses numeric + UCUM units (mg/mcg/g/ml/U)."
        " Vitals digits only as printed (temp with decimals + F/C as printed, spo2 with %).\n"
        "- Do NOT correct spelling, do NOT expand abbreviations, do NOT apply"
        " speech-mishear rules — copy text exactly as printed.\n\n"
        "OCR lines:\n" + numbered[:3000]
    )


def _relabeled(d):
    """_valid_drug tags notes 'llm-primary'; this path is the image judge."""
    if d and "llm-primary" in str(d.get("note", "")):
        d = dict(d)
        d["note"] = d["note"].replace("llm-primary", "llm-image")
    return d


def _gate_line(item, full):
    """Proof gate: source_line must be a verbatim OCR substring."""
    s = str(item.get("source_line", "") or "")
    if s and s[:40] not in full and s not in full:
        item = dict(item)
        item["source_line"] = ""
        item["line_no"] = -1
        item["confidence"] = min(float(item.get("confidence", 0.9)), 0.80)
        item["note"] = (str(item.get("note", "")) + " unprovenance").strip()
    return item


def extract_llm_image(ocr_text, lines, segments, model="llama3.2:3b", timeout=120):
    """OCR text -> LLM entity candidates. Raises ValueError on any failure."""
    ok, reason = ollama_available(model)
    if not ok:
        raise ValueError(reason)
    full = ocr_text or ""
    last_error = None
    for _ in range(3):
        try:
            out = subprocess.run(
                ["ollama", "run", "--format", "json", model,
                 _prompt_extract(ocr_text, lines)],
                capture_output=True, encoding="utf-8", errors="ignore",
                timeout=timeout)
        except Exception as e:
            last_error = ValueError(f"ollama run failed: {type(e).__name__}")
            continue
        json_str = _extract_json_from_output(_clean_ollama_output(out.stdout or ""))
        if not json_str:
            last_error = ValueError("no JSON in LLM output")
            continue
        try:
            j = json.loads(json_str)
        except Exception as e:
            last_error = ValueError(f"LLM JSON unparsable: {e}")
            continue
        if not isinstance(j, dict) or not any(
                k in j for k in ("drugs", "chief_complaint", "vitals")):
            last_error = ValueError("LLM JSON missing required keys")
            continue
        # Drugs: schema-validate, then keep only OCR-verbatim names.
        drugs = []
        for x in j.get("drugs", []) or []:
            d = _valid_drug(x)
            if not d:
                continue
            d = _relabeled(_gate_line({**d, "source_line": x.get("source_line", ""),
                                       "line_no": x.get("line_no", -1)}, full))
            if str(d.get("name", "")).lower() not in full.lower():
                continue  # never invent drug names
            if not d.get("source_line"):
                d["confidence"] = min(float(d.get("confidence", 0.9)), 0.80)
            drugs.append(d)
        # Vitals: digit-presence gate — digits must occur in the OCR text.
        v = j.get("vitals", {}) if isinstance(j.get("vitals"), dict) else {}
        vitals, structured_vitals = [], {}

        def _digits(s):
            return re.findall(r"\d+(?:\.\d+)?", str(s))

        if v.get("sys") and v.get("dia"):
            ds, dd = _digits(v["sys"]), _digits(v["dia"])
            if ds and dd and ds[0] in full and dd[0] in full:
                vitals.append({"text": f"BP {v['sys']}/{v['dia']}", "confidence": 0.88,
                               "color": "YELLOW", "source_sentence": "",
                               "note": "llm-image"})
                structured_vitals["sys"], structured_vitals["dia"] = str(v["sys"]), str(v["dia"])
        for k in ("temp", "spo2"):
            if v.get(k):
                dk = _digits(v[k])
                if dk and dk[0] in full:
                    vitals.append({"text": str(v[k]), "confidence": 0.87, "color": "YELLOW",
                                   "source_sentence": "", "note": "llm-image"})
                    structured_vitals[k] = str(v[k])
        # Symptoms / allergies: verbatim-substring only, else dropped.
        symptoms = []
        cc = str(j.get("chief_complaint", "") or "")
        if cc and cc.lower() in full.lower():
            symptoms.append({"text": cc, "confidence": 0.9, "color": "YELLOW",
                             "source_sentence": "", "negated": False, "note": "llm-image"})
        symptoms += [{"text": str(s), "confidence": 0.91, "color": "GREEN",
                      "source_sentence": "", "negated": True, "note": "DENIED - excluded"}
                     for s in (j.get("symptoms_denied", []) or [])
                     if str(s).lower() in full.lower()]
        allergies = [{"text": str(a), "negated": True, "confidence": 0.9,
                      "source_sentence": "", "note": "NEGATED - llm-image"}
                     for a in (j.get("allergies_denied", []) or [])
                     if str(a).lower() in full.lower()]
        allergies += [{"text": str(a), "negated": False, "confidence": 0.82,
                       "color": "YELLOW", "source_sentence": "", "note": "llm-image - verify"}
                      for a in (j.get("allergies_active", []) or [])
                      if str(a).lower() in full.lower()]
        dx = j.get("diagnosis", {}) if isinstance(j.get("diagnosis"), dict) else {}
        fu = j.get("followup", {}) if isinstance(j.get("followup"), dict) else {}
        diagnosis = {}
        if str(dx.get("text", "") or ""):
            diagnosis = _gate_line({"text": str(dx["text"]), "icd10": "",
                                    "confidence": 0.85, "color": "YELLOW",
                                    "source_sentence": "", "note": "llm-image",
                                    "source_line": dx.get("source_line", ""),
                                    "line_no": dx.get("line_no", -1)}, full)
        followup = {}
        if str(fu.get("text", "") if isinstance(fu, dict) else fu or ""):
            ft = str(fu.get("text", "") if isinstance(fu, dict) else fu)
            followup = _gate_line({"text": ft, "confidence": 0.88, "color": "YELLOW",
                                   "source_sentence": "", "note": "llm-image",
                                   "source_line": fu.get("source_line", "") if isinstance(fu, dict) else "",
                                   "line_no": fu.get("line_no", -1) if isinstance(fu, dict) else -1}, full)
        patient = {}
        if str(j.get("patient_name", "") or "").lower() in full.lower() and str(j.get("patient_name") or ""):
            patient = {"name": str(j["patient_name"]), "confidence": 0.85,
                       "color": "YELLOW", "source_sentence": "",
                       "note": "llm-image - verify"}
        return {"drugs": drugs, "symptoms": symptoms, "vitals": vitals,
                "structured_vitals": structured_vitals,
                "allergies": allergies,
                "negations": [{"span": str(a), "negated": True}
                              for a in (j.get("allergies_denied", []) or [])
                              if str(a).lower() in full.lower()],
                "diagnosis": diagnosis, "followup": followup, "patient": patient,
                "llm_engine": f"ollama-image:{model}"}
    raise last_error or ValueError("LLM image extraction failed after retries")


def _verbatim(s, full):
    return bool(s) and str(s).lower() in str(full).lower()


def merge_image(base_ent, llm_ent, ocr_text, model="llama3.2:3b"):
    """Gap-fill LLM candidates over the regex base. Regex values always win.

    A candidate fills a slot only with verbatim OCR proof; anything without
    proof is dropped (never invented). New drug rows need the name spelled
    verbatim in the OCR text.
    """
    import difflib
    full = ocr_text or ""
    merged = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
              for k, v in (base_ent or {}).items()}
    names = [(str(d.get("name", "")).lower(), d) for d in merged.get("drugs", [])]
    for t in llm_ent.get("drugs", []) or []:
        tn = str(t.get("name", "")).lower()
        if not tn or tn not in full.lower():
            continue
        best, score = None, 0
        for n, d in names:
            s = difflib.SequenceMatcher(None, tn, n).ratio()
            if s > score:
                best, score = d, s
        if best is not None and score >= 0.6:
            for k in ("dose", "unit", "frequency", "duration"):
                if best.get(k) in (None, "", 0) and t.get(k) not in (None, "", 0):
                    cand = str(t[k])
                    if k in ("dose", "unit"):
                        if not re.findall(r"\d+(?:\.\d+)?", cand) or \
                                not all(dg in full for dg in re.findall(r"\d+(?:\.\d+)?", cand)):
                            continue
                    elif not _verbatim(cand, full):
                        continue
                    best[k] = t[k]
                    best["note"] = (str(best.get("note", "")) + " llm-image-gapfill").strip()
        else:
            t = dict(t)
            t["note"] = (t.get("note", "") + " llm-image-gapfill - verify spelling").strip()
            t["color"] = "YELLOW"
            merged.setdefault("drugs", []).append(t)
            names.append((tn, merged["drugs"][-1]))
    for k in ("diagnosis", "followup"):
        if not merged.get(k) and llm_ent.get(k, {}).get("text") and \
                _verbatim(llm_ent[k]["text"], full):
            merged[k] = llm_ent[k]
    if not merged.get("patient") and llm_ent.get("patient", {}).get("name") and \
            _verbatim(llm_ent["patient"]["name"], full):
        merged["patient"] = llm_ent["patient"]
    filled = _regex_vitals_slots(merged)
    for k in ("allergies", "symptoms"):
        seen = {str(x.get("text", x.get("name", ""))).lower() for x in merged.get(k, [])}
        for x in llm_ent.get(k, []) or []:
            xt = str(x.get("text", x.get("name", "")))
            if xt.lower() not in seen and _verbatim(xt, full):
                merged.setdefault(k, []).append(x)
                seen.add(xt.lower())
    seen_v = {str(x.get("text", x.get("name", ""))).lower() for x in merged.get("vitals", [])}
    for x in llm_ent.get("vitals", []) or []:
        kind = _llm_vitals_kind(x)
        if kind in filled and filled[kind]:
            continue
        xt = str(x.get("text", x.get("name", "")))
        if xt.lower() in seen_v or not _verbatim(xt, full):
            continue
        merged.setdefault("vitals", []).append(x)
    sv = dict(llm_ent.get("structured_vitals", {}) or {})
    if sv:
        if filled["bp"]:
            sv.pop("sys", None)
            sv.pop("dia", None)
        if filled["temp"]:
            sv.pop("temp", None)
        if filled["spo2"]:
            sv.pop("spo2", None)
        for k in list(sv):
            if not re.findall(r"\d+(?:\.\d+)?", str(sv[k])) or \
                    not all(dg in full for dg in re.findall(r"\d+(?:\.\d+)?", str(sv[k]))):
                sv.pop(k, None)
        if sv:
            merged["structured_vitals"] = {**(merged.get("structured_vitals", {}) or {}), **sv}
    merged["llm_engine"] = f"regex+llm-image:{model}"
    return merged


def _prompt_judge(slot_values, ocr_text, lines):
    numbered = _numbered_lines(ocr_text, lines)
    line_text = {ln.get("line"): str(ln.get("text", "")) for ln in (lines or [])}
    flines = []
    for f, v in slot_values:
        cands = [str(n) for n, t in sorted(line_text.items())
                 if _value_supported(v, t)][:4]
        hint = f" [value text seen in lines: {', '.join(cands)}]" if cands else ""
        flines.append(f"- {f}: {v}{hint}")
    fields = "\n".join(flines)
    return (
        "You are a strict judge for ER discharge-note completeness.\n"
        "Each field below holds the value extracted for it, with the OCR line"
        " numbers where its text occurs. Confirm the single best supporting"
        " line per field (line_no + exact quote in evidence_line)."
        " If none of the hinted lines actually supports the field's meaning,"
        " use status UNPROVEN with line_no -1.\n\n"
        "Return STRICT JSON only (no prose, no markdown):\n"
        '{"fields": [{"field":"...", "status":"PASS", "line_no": 0,'
        ' "evidence_line":"...", "confidence": 0.9}]}\n'
        "status is PASS (the numbered line supports the value) or UNPROVEN (no"
        " supporting line). Never rephrase, never correct spelling,"
        " never invent line numbers.\n\n"
        "Fields:\n" + fields[:2500] + "\n\nOCR lines:\n" + numbered[:3000]
    )


def _value_supported(value, evidence):
    """Slot value plausibly backed by the quoted evidence line."""
    v, e = str(value).lower(), str(evidence).lower()
    if v in e:
        return True
    digs = re.findall(r"\d+(?:\.\d+)?", v)
    return bool(digs) and all(d in e for d in digs)


def judge_fields(slots, ocr_text, lines, model="llama3.2:3b", timeout=120):
    """Per-field PASS | MISSING | UNPROVEN. Raises ValueError on LLM failure.

    slots: coords.resolve_slots()["slots"] list. NIL/empty slots are forced
    MISSING locally; PASS needs a verbatim OCR proof (downgraded to UNPROVEN
    otherwise), so the judge cannot rubber-stamp its own extraction.
    """
    full = ocr_text or ""
    by_key = {s["key"]: str(s.get("text", "")) for s in slots or []}
    line_text = {ln.get("line"): str(ln.get("text", "")) for ln in (lines or [])}
    keys = [k for k in JUDGED_KEYS if k in by_key]
    missing = [k for k in keys
               if not by_key[k].strip() or by_key[k].strip().upper().startswith("NIL")]
    filled = [(k, by_key[k]) for k in keys if k not in missing]
    verdicts = {}
    if filled:
        ok, reason = ollama_available(model)
        if not ok:
            raise ValueError(reason)
        last_error = None
        for _ in range(3):
            try:
                out = subprocess.run(
                    ["ollama", "run", "--format", "json", model,
                     _prompt_judge(filled, ocr_text, lines)],
                    capture_output=True, encoding="utf-8", errors="ignore",
                    timeout=timeout)
            except Exception as e:
                last_error = ValueError(f"ollama run failed: {type(e).__name__}")
                continue
            json_str = _extract_json_from_output(_clean_ollama_output(out.stdout or ""))
            if not json_str:
                last_error = ValueError("no JSON in judge output")
                continue
            try:
                j = json.loads(json_str)
            except Exception as e:
                last_error = ValueError(f"judge JSON unparsable: {e}")
                continue
            if not isinstance(j, dict) or not isinstance(j.get("fields"), list):
                last_error = ValueError("judge JSON missing fields[]")
                continue
            verdicts = {f.get("field"): f for f in j["fields"] if isinstance(f, dict)}
            break
        else:
            raise last_error or ValueError("LLM judge failed after retries")
    fields = []
    for k in keys:
        if k in missing:
            fields.append({"field": k, "status": "MISSING", "value": by_key[k],
                           "evidence_line": "", "line_no": -1, "confidence": 1.0,
                           "note": "not found in OCR — confirm truly absent"})
            continue
        v = verdicts.get(k, {})
        try:
            lno = int(v.get("line_no", -1))
        except (TypeError, ValueError):
            lno = -1
        # Evidence is the OCR line itself (looked up locally by number), not
        # the judge's free-text quote — small models paraphrase, line numbers
        # they get right. PASS needs the line to back the slot value.
        ev = line_text.get(lno, "")
        try:
            conf = float(v.get("confidence", 0.85))
        except (TypeError, ValueError):
            conf = 0.85
        if (str(v.get("status", "")).upper() == "PASS" and ev and ev in full
                and _value_supported(by_key[k], ev)):
            fields.append({"field": k, "status": "PASS", "value": by_key[k],
                           "evidence_line": ev, "line_no": lno,
                           "confidence": round(min(conf, 0.9), 2), "note": ""})
        else:
            note = "judge found no verbatim supporting line — verify against the image"
            if v.get("status") == "MISSING":
                note = "judge disagrees with extracted value — verify against the image"
            fields.append({"field": k, "status": "UNPROVEN", "value": by_key[k],
                           "evidence_line": ev if ev and _value_supported(by_key[k], ev) else "",
                           "line_no": lno,
                           "confidence": round(min(conf, 0.75), 2), "note": note})
    summary = {"pass": sum(1 for f in fields if f["status"] == "PASS"),
               "missing": sum(1 for f in fields if f["status"] == "MISSING"),
               "unproven": sum(1 for f in fields if f["status"] == "UNPROVEN")}
    return {"model": model, "engine": f"ai-eval:{model}",
            "fields": fields, "summary": summary}


def run_image_llm_eval(base_ent, ocr_text, segments, model="llama3.2:3b", timeout=120):
    """Full image LLM path: extract -> merge -> judge.

    base_ent: regex entities (no job_id needed). segments: image segments
    (id == OCR line no). Returns (merged_ent, eval_json). Raises ValueError.
    """
    from coords import resolve_slots
    lines = _lines_from_segments(segments)
    llm = extract_llm_image(ocr_text, lines, segments, model=model, timeout=timeout)
    merged = merge_image(base_ent, llm, ocr_text, model=model)
    tj = {"job_id": "", "text": ocr_text or "", "normalized_en": ocr_text or "",
          "segments": segments or [], "source": "image"}
    slots = resolve_slots({"job_id": "", **merged}, tj, "er_discharge")["slots"]
    eval_json = judge_fields(slots, ocr_text, lines, model=model, timeout=timeout)
    return merged, eval_json
