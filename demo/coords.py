"""Coordinate resolver: entity words -> absolute mm slots (single A4 strictly).

Implements the 4-step model: base unit mm (A4 210x297, origin top-left),
geometric X/Y per slot, isolation bounding boxes, font/overflow guards.
"""
import json
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
MM_PER_PT = 25.4 / 72  # 0.3528


def load_coords(template_id="er_discharge"):
    with open(os.path.join(_HERE, "templates", f"{template_id}.coords.json"), encoding="utf-8") as f:
        return json.load(f)


def prefix_width_mm(prefix, font_pt=9):
    """Inline-insertion X offset: measured prefix width, monospace ch fallback."""
    try:
        return len(prefix) * 0.6 * font_pt * MM_PER_PT
    except Exception:
        return len(prefix) * 1.9


def fit_text(text, max_chars, overflow, font_pt=9):
    """Returns (display_text, font_pt, truncated_flag).

    The discharge note flows like a document (no fixed-size boxes), so a
    clinical value is never cut: the full text is returned and the flag is
    always False. max_chars/overflow in the coords file are kept only for
    compatibility with older callers.
    """
    return ("" if text is None else str(text)), font_pt, False


NIL_MISSING = "NIL"
DEDUP = {"khansi": "cough", "bukhar": "fever"}


def _fmt_dose(d):
    """Returns (dose_text, dose_color, unit_text, unit_color). Never None/''."""
    dose, unit = d.get("dose"), (d.get("unit") or "").strip()
    if dose is None or dose == "" or dose == 0:
        dose_text, dose_color = NIL_MISSING, "RED"
    else:
        try:
            dose_text = str(int(float(dose))) if float(dose).is_integer() else str(dose)
        except (TypeError, ValueError):
            dose_text = str(dose)
        dose_color = d.get("color", "YELLOW")
    unit = {"units": "U", "unit": "U"}.get(unit.lower(), unit) if unit else unit
    if not unit:
        unit_text, unit_color = NIL_MISSING, "RED"
    else:
        unit_text, unit_color = unit, d.get("color", "YELLOW")
    return dose_text, dose_color, unit_text, unit_color


_TEMP_RE = r"\d{2,3}(?:\.\d{1,2})?\s*(?:degrees?(?:\s*(?:fahrenheit|celsius|F|C))?|°\s*[FC]?|fahrenheit|celsius)"
_BP_RE = r"(\d{2,3}(?:\.\d{1,2})?)\s*(by|/|over|of)\s*(\d{2,3}(?:\.\d{1,2})?)"


# BP read from free text (fallback, AI gate, judge) needs an explicit BP label
# shortly before it and must not be part of a date (05/05/2023, 12/10/24).
_BP_CTX_RE = re.compile(
    r"(?:\bBPA?\b|\bB\.\s?P\.?|\bblood\s+pressure\b)[^\n.]{0,40}?"
    r"(?<![\d/.])(\d{2,3}(?:\.\d{1,2})?)\s*(?:by|/|over|of)\s*(\d{2,3}(?:\.\d{1,2})?)(?![\d/])",
    re.I)


def bp_readings(text):
    """[(sys, dia, matched_text)] for BP-labelled, non-date, plausible readings in text."""
    out = []
    for m in _BP_CTX_RE.finditer(str(text or "")):
        s, d = float(m.group(1)), float(m.group(2))
        if 50 <= s <= 300 and 20 <= d <= 200 and s > d:
            out.append((m.group(1), m.group(2), m.group(0)))
    return out


def bp_supported(sys_v, dia_v, text):
    """True if text documents this exact BP with a BP label (dates never count)."""
    try:
        s, d = float(str(sys_v).strip()), float(str(dia_v).strip())
    except (TypeError, ValueError):
        return False
    return any(float(a) == s and float(b) == d for a, b, _ in bp_readings(text))


_FIELD_KIND = {"sys": "bp", "dia": "bp", "temp": "temp", "spo2": "spo2"}
_KIND_LABEL = {"bp": "BP", "temp": "temperature", "spo2": "SpO2"}


def _parse_vital_text(t):
    """One extracted vitals string -> (kind, {box field: value}) or None."""
    m = re.search(_BP_RE, t)
    if m:
        return "bp", {"sys": m.group(1), "dia": m.group(3)}
    if re.search(r"degree|temp|fahrenheit|celsius|°", t, re.I):
        mt = re.search(_TEMP_RE, t, re.I)
        # full reading, never "Fahren"; "Temp: 99.2 F" (bare unit letter) -> "99.2 F"
        mt = mt or re.search(r"\d{2,3}(?:\.\d{1,2})?\s*°?\s*[FC]\b", t)
        return "temp", {"temp": mt.group(0) if mt else t[:40]}
    if re.search(r"spo2|o2|%|oxygen|percent", t, re.I):
        ms = re.search(r"\d{2,3}(?:\.\d{1,2})?\s*(?:%|percent)?", t, re.I)
        return "spo2", {"spo2": ms.group(0).strip() if ms else t[:40]}
    return None


def regex_vital_readings(vitals, vtext=""):
    """Every DISTINCT regex reading per vital, in order of mention.

    {"bp"|"temp"|"spo2": [{"value", "values", "sources", "count"}]}. The same
    value mentioned again (repeat measurement / restatement) is one reading
    with count>1 and all its source sentences; a different value (or a
    contradicting F/C unit) is a separate reading. No unit conversion and no
    "preferred" reading. vtext (normalized transcript) is used only when no
    vitals entry parsed, mirroring regex_vital_boxes.
    """
    from llm_extract import _same_vital
    out = {"bp": [], "temp": [], "spo2": []}

    def add(kind, values, source):
        for r in out[kind]:
            if all(_same_vital(f, r["values"][f], values[f]) for f in values):
                r["count"] += 1
                if source and source not in r["sources"]:
                    r["sources"].append(source)
                return
        value = f"{values['sys']}/{values['dia']}" if kind == "bp" else next(iter(values.values()))
        out[kind].append({"value": value, "values": values,
                          "sources": [source] if source else [], "count": 1})

    for v in vitals or []:
        if not isinstance(v, dict):
            continue
        p = _parse_vital_text(v.get("text", ""))
        if p:
            add(p[0], p[1], str(v.get("source_sentence", "") or v.get("text", "")).strip())
    if vtext and not out["bp"]:
        for sys_v, dia_v, src in bp_readings(vtext):  # BP-labelled only, never a date
            add("bp", {"sys": sys_v, "dia": dia_v}, src)
    if not any(out.values()) and vtext:
        for m in re.finditer(r"\b" + _TEMP_RE, vtext, re.I):
            add("temp", {"temp": m.group(0)}, m.group(0))
    return out


def regex_vital_boxes(vitals, vtext=""):
    """Regex readings -> {sys, dia, temp, spo2} (decimal-aware, 'of' separator, long-form BP).

    Always the numeric reading, never the whole "temp is currently ..." sentence.
    vtext (normalized transcript) is scanned only when no vitals entry parsed.
    """
    boxes = {"sys": "", "dia": "", "temp": "", "spo2": ""}
    for v in vitals or []:
        p = _parse_vital_text(v.get("text", "") if isinstance(v, dict) else "")
        if p:
            boxes.update(p[1])  # last mention fills the box (see regex_vital_readings)
    if vtext and not boxes["sys"] and not boxes["dia"]:
        bps = bp_readings(vtext)  # BP-labelled only, never a date
        if bps:
            boxes["sys"], boxes["dia"] = bps[0][0], bps[0][1]
    if not any(boxes.values()) and vtext:
        mt = re.search(r"\b" + _TEMP_RE, vtext, re.I)
        if mt:
            boxes["temp"] = mt.group(0)
    return boxes


AI_NOTE = "AI-suggested - verify"  # provenance label for unreviewed AI values
# clinician resolutions of an unrecognised medication mention (editor choices)
UNREC_ADDED = "prescribed, added to the medication list"
UNREC_DISMISSED = "not a prescribed medicine"
ALLERGY_NKA_TEXT = "No known allergies (documented) - verify"
ALLERGY_MISSING_TEXT = "NOT DOCUMENTED - confirm allergy status"
NKA_MARKER_SPAN = "No allergy"  # stt_extract.NKA_MARKER: explicit NKA statement


def allergy_view(ents):
    """Allergy line for the note. Never invents or infers allergy status.

    - documented allergens (regex or AI, always YELLOW = verify) are listed;
    - "no known allergies" only when explicitly documented (NKA marker) or set
      by the clinician (allergy_status="none_known");
    - nothing documented -> NOT DOCUMENTED (never NKA);
    - NKA + an allergen, or one allergen both active and denied -> RED
      conflict for clinician review (nothing is dropped or chosen).
    A clinician edit (allergy_status set) is authoritative.
    """
    items = []
    for a in ents.get("allergies", []) or []:
        if isinstance(a, str):  # free-form LLM tidy output: keep, mark unverified
            a = {"text": a, "negated": False, "color": "YELLOW", "ai_suggested": True}
        if isinstance(a, dict) and str(a.get("text", "") or "").strip():
            items.append(a)
    active = [a for a in items if not a.get("negated")]
    denied = [a for a in items if a.get("negated")]
    status = ents.get("allergy_status")  # clinician: "none_known" | "reviewed"
    nka_doc = any(isinstance(n, dict) and n.get("span") == NKA_MARKER_SPAN
                  for n in ents.get("negations", []) or [])
    nka = status == "none_known" or (status is None and nka_doc)
    conflicts = []
    if status is None:
        if nka and active:
            conflicts.append("'no known allergies' documented but allergy to "
                             + ", ".join(a["text"] for a in active) + " also documented")
        both = sorted({a["text"].lower() for a in active} & {a["text"].lower() for a in denied})
        if both:
            conflicts.append("documented as both allergic and not allergic: " + ", ".join(both))
    conflict = "; ".join(conflicts)
    if active:
        text = "; ".join(a["text"] for a in active)
        color = "RED" if conflict else "YELLOW"
    elif nka:
        text, color = ALLERGY_NKA_TEXT, "RED" if conflict else "YELLOW"
    else:
        text, color = ALLERGY_MISSING_TEXT, "RED" if conflict else "YELLOW"
    note = f"CONFLICT - clinician review: {conflict}" if conflict else ""
    ai_names = [a["text"] for a in active if a.get("ai_suggested")]
    if ai_names:  # AI-sourced allergens stay listed but labelled for review
        note = "; ".join(x for x in (f"{AI_NOTE}: " + ", ".join(ai_names), note) if x)
    return {"text": text, "color": color, "note": note, "conflict": conflict,
            "active": active, "denied": denied, "nka": nka}


def resolve_slots(entities, transcript, template_id="er_discharge"):
    coords = load_coords(template_id)
    S = coords["slots"]
    ents = entities or {}
    slots, denied, proof = [], [], []

    def slot(key, text, color="YELLOW", confidence="", note="", chip=False, ai=False):
        c = dict(S[key])
        disp, fpt, trunc = fit_text(text, c["max_chars"], c["overflow"])
        is_nil = disp.startswith("NIL")
        if ai and not is_nil:  # unreviewed AI value: never GREEN, always labelled
            color = "YELLOW" if color == "GREEN" else color
            note = "; ".join(x for x in (AI_NOTE, note) if x)
        d = {"key": key, "x": c["x"], "y": c["y"], "w": c["w"], "h": c.get("h"),
             "align": c["align"], "overflow": c["overflow"], "text": disp,
             "color": color, "confidence": confidence, "note": note,
             "font_pt": fpt if fpt != 9 else "", "truncated": trunc,
             "extra": ("blank " if c.get("style") == "blank_underline" else "")
                      + ("redbox " if color == "RED" and (not disp or disp in ("___",) or is_nil) else "")
                      + ("nil-red " if is_nil and color == "RED" else "")
                      + ("nil-green " if is_nil and color == "GREEN" else "")}
        if chip:
            d["chip"] = True
            d["chip_x"] = 174
        return d

    # patient name: image source only (required there; audio notes get no field)
    if (transcript or {}).get("source") == "image" and "patient_name" in S:
        pt = ents.get("patient") or {}
        pn = str(pt.get("name", "") or "").strip()
        slots.append(slot("patient_name", pn or "NIL — not found", "YELLOW" if pn else "RED",
                          pt.get("confidence", "") if pn else "", ai=bool(pt.get("ai_suggested"))))

    # chief complaint: first non-negated symptom, deduped
    seen = set()
    for s in ents.get("symptoms", []):
        if s.get("negated"):
            continue
        key = DEDUP.get(s.get("text", "").lower(), s.get("text", "").lower())
        if key in seen:
            continue
        seen.add(key)
        slots.append(slot("cc_blank", key, s.get("color", "YELLOW"), s.get("confidence", ""),
                          ai=bool(s.get("ai_suggested"))))
        break
    else:
        slots.append(slot("cc_blank", "NIL — not heard", "RED", ""))

    # HPI inline insertions: keyword X = sentence origin + prefix width
    full = transcript.get("text", "")
    inline_keys = ["hpi_inline_1", "hpi_inline_2"]
    found, seen_inline = 0, set()
    for s in ents.get("symptoms", []) + ents.get("drugs", []):
        if found >= 2:
            break
        word = s.get("name") or s.get("text", "")
        canon = DEDUP.get(word.lower(), word.lower())
        if canon in seen_inline or not word:
            continue
        idx = full.lower().find(word.lower())
        if idx < 0:
            continue
        seen_inline.add(canon)
        prefix = full[:idx]
        col_chars = len(prefix) % 94
        line = min(len(prefix) // 94, 1)
        c = dict(S[inline_keys[found]])
        c["x"] = round(min(max(15 + prefix_width_mm(prefix[-col_chars:] if col_chars else ""), 15), 170), 1)
        c["y"] = 66 + line * 6
        disp, fpt, _ = fit_text(word, c["max_chars"], c["overflow"])
        slots.append({"key": inline_keys[found], "x": c["x"], "y": c["y"], "w": c["w"],
                      "h": c.get("h"), "align": c["align"], "overflow": c["overflow"],
                      "text": disp, "color": s.get("color", "YELLOW"),
                      "confidence": s.get("confidence", ""), "note": "",
                      "font_pt": fpt if fpt != 9 else "", "extra": ""})
        found += 1

    # drug rows: EVERY active medicine gets a row (never silently dropped);
    # rows beyond the coords file reuse the last row's slot settings
    active = [d for d in ents.get("drugs", []) if not d.get("negated")]
    for i in range(2, len(active)):
        for col in ("name", "dose", "unit", "freq", "duration"):
            S.setdefault(f"drug_{i}_{col}", dict(S[f"drug_1_{col}"]))
    drug_rows = []
    if not active:
        row = {"index": 0, "cells": {}}
        row["cells"]["name"] = slot("drug_0_name", "NIL — no recognised medicine (see list below)"
                                     if ents.get("unrecognised_meds") else "NIL — no medicine documented",
                                     "RED", "", chip=True)
        slots.append(row["cells"]["name"])
        for col in ("dose", "unit", "freq", "duration"):
            if f"drug_0_{col}" in S:
                row["cells"][col] = slot(f"drug_0_{col}", NIL_MISSING, "RED", "")
                slots.append(row["cells"][col])
        drug_rows.append(row)
    for i, d in enumerate(active):
        drug_rows.append({"index": i + 1, "cells": {}})
        dose_text, dose_color, unit_text, unit_color = _fmt_dose(d)
        vals = {"name": (d.get("name", "") or NIL_MISSING),
                "dose": dose_text, "unit": unit_text,
                "freq": d.get("frequency", "") or NIL_MISSING,
                "duration": d.get("duration", "") or NIL_MISSING}
        cols = {"name": f"drug_{i}_name", "dose": f"drug_{i}_dose", "unit": f"drug_{i}_unit",
                "freq": f"drug_{i}_freq", "duration": f"drug_{i}_duration"}
        for col in ("name", "dose", "unit", "freq", "duration"):
            key = cols[col]
            if key not in S:
                continue
            cell_color = {"dose": dose_color, "unit": unit_color}.get(col, d.get("color", "YELLOW"))
            if vals[col] == NIL_MISSING:
                cell_color = "RED"
            field = {"freq": "frequency"}.get(col, col)
            s = slot(key, vals[col], cell_color, d.get("confidence", ""),
                     note=d.get("note", "") if col == "name" else "",
                     chip=(col == "duration" or vals[col] == NIL_MISSING),
                     ai=bool(d.get("ai_suggested")) or field in (d.get("ai_fields") or []))
            slots.append(s)
            drug_rows[-1]["cells"][col] = s

    # vitals boxes: regex readings first (normalized text fallback), then
    # structured_vitals per field. structured_vitals holds LLM gap-fills (only
    # for slots regex left empty, see merge_primary/merge_image) and human
    # corrections from the editable form; a field it does not carry keeps its
    # regex value ("" = explicitly cleared -> NIL/RED).
    vtext = transcript.get("normalized_en") or full
    boxes = regex_vital_boxes(ents.get("vitals", []), vtext)
    sv = ents.get("structured_vitals") or {}
    if not isinstance(sv, dict):
        sv = {}
    sv_ai = set(ents.get("structured_vitals_ai") or [])  # AI gap-filled boxes (unreviewed)
    for k in boxes:
        if k in sv:
            boxes[k] = str(sv.get(k) or "")
    # regex vs AI disagreements are kept visible until a human sets the field
    conflicts = {c.get("field"): c for c in ents.get("vitals_conflicts", []) or []
                 if isinstance(c, dict) and c.get("field") not in sv}
    # different readings of one vital: the box holds the last mention, so warn
    # and list them all; nothing is chosen as "preferred". Cleared per vital
    # once a human sets all its fields.
    multi = {kind: rs for kind, rs in regex_vital_readings(ents.get("vitals", []), vtext).items()
             if len(rs) > 1 and any(f not in sv for f, kd in _FIELD_KIND.items() if kd == kind)}
    vmap = {"sys": "vitals_bp_sys", "dia": "vitals_bp_dia", "temp": "vitals_temp", "spo2": "vitals_spo2"}
    for k, key in vmap.items():
        c = conflicts.get(k)
        rs = multi.get(_FIELD_KIND[k]) if k not in sv else None
        notes = []
        if rs:
            notes.append("multiple readings - verify: " + "; ".join(r["value"] for r in rs))
        if c:
            notes.append(f"AI read {c.get('ai', '')} - verify")
        slots.append(slot(key, boxes[k] or NIL_MISSING, "YELLOW" if boxes[k] else "RED", "",
                          note="; ".join(notes), ai=k in sv and k in sv_ai))

    # allergies: active line + grey DENIED floats (see allergy_view for the rules)
    av = allergy_view(ents)
    s_al = slot("allergy_line", av["text"], av["color"], "", note=av["note"])
    if s_al["truncated"]:  # box holds 60 chars: keep every allergen in the note (DOCX prints it)
        s_al["note"] = "; ".join(x for x in (f"all: {av['text']}", av["note"]) if x)
    slots.append(s_al)
    dy = 154.0
    for a in av["denied"]:
        denied.append({"x": 30, "y": round(dy, 1), "w": 150, "text": f"{a.get('text','')}"[:40]})
        dy += 2.8
    for s in [x for x in ents.get("symptoms", []) if x.get("negated")]:
        denied.append({"x": 30, "y": round(dy, 1), "w": 150, "text": f"{s.get('text','')}"[:40]})
        dy += 2.8

    # diagnosis + follow-up + date/sign
    dx = ents.get("diagnosis") or {}
    fu = ents.get("followup") or {}
    slots.append(slot("dx_text", dx.get("text") or "NIL — [DR CONFIRM]", "YELLOW" if dx.get("text") else "RED", "",
                      ai=bool(dx.get("ai_suggested"))))
    slots.append(slot("dx_code_box", dx.get("icd10") or "NIL", "YELLOW" if dx.get("icd10") else "RED", ""))
    dx_fu = fu.get("text", "")
    if "fu_text" in S:
        # missing follow-up instructions are a review item, not "fine" (GREEN)
        slots.append(slot("fu_text", dx_fu or "NIL — no follow-up documented",
                           "YELLOW", "", ai=bool(fu.get("ai_suggested"))))
    slots.append(slot("date_box", __import__("datetime").date.today().isoformat(), "GREEN", ""))
    slots.append(slot("sign_line", ents.get("signature") or "________________", "GREEN", ""))

    # proof micro-strip: max 2 lines, single page strictly
    segs = transcript.get("segments", [])
    span = f"seg {segs[0].get('id', 0)} {segs[0].get('start', 0)}-{segs[0].get('end', 0)}s" if segs else ""
    heard = (active[0].get("source_sentence", "") if active else full)[:130]
    proof.append(f"Heard: \"{heard}\" [{span}]")
    misc = []
    if av["conflict"]:
        misc.append(f"ALLERGY CONFLICT (verify): {av['conflict']}")
    if not any(boxes.values()):
        m = re.search(r"BPA?[^.,]{0,30}", full, re.I)
        if m:
            misc.append(f"unmapped vitals: \"{m.group(0).strip()}\"")
    misc += [f"MULTIPLE {_KIND_LABEL[kd]} readings (verify): " + " | ".join(r["value"] for r in rs)
             for kd, rs in multi.items()]
    misc += [f"VITAL CONFLICT {k}: transcript {c.get('regex', '')} vs AI {c.get('ai', '')}"
             for k, c in conflicts.items()]
    misc += [f"DENIED: {d['text']}" for d in denied]
    if dx_fu:
        misc.append(f"Follow-up: {dx_fu}"[:60])
    if misc:
        proof.append("; ".join(misc)[:180])
    # complete, untruncated review notes for the document (proof_lines is the legacy strip)
    review_notes = [ln for ln in misc if not ln.startswith(("DENIED: ", "Follow-up: "))]
    # unrecognised medication mentions: each is a RED slot (blocks Verify) until the
    # clinician explicitly resolves it - "added" only counts when a medicine row was
    # actually added by hand; "dismissed" = not a prescribed medicine
    unrec_ents = [u for u in ents.get("unrecognised_meds", []) or [] if isinstance(u, dict)]
    unrec = [u.get("text", "") for u in unrec_ents]
    n_added_rows = sum(1 for d in active if d.get("note") == "human-added")
    n_added_ok, unrec_items = 0, []
    for i, u in enumerate(unrec_ents):
        S.setdefault(f"unrec_{i}", dict(S["dx_text"]))
        res, text = u.get("resolution"), u.get("text", "")
        if res == "added" and n_added_ok < n_added_rows:
            n_added_ok += 1
            disp, color, note = f"{text} → RESOLVED: {UNREC_ADDED}", "YELLOW", "resolved by clinician"
        elif res == "dismissed":
            disp, color, note = f"{text} → RESOLVED: {UNREC_DISMISSED}", "YELLOW", "resolved by clinician"
        elif res == "added":
            disp, color, note = text, "RED", "marked as added, but no medicine row was added - add it above"
        else:
            disp, color, note = text, "RED", "unrecognised medication - clinician must resolve"
        s = slot(f"unrec_{i}", disp, color, "", note)
        slots.append(s)
        unrec_items.append(s)
        review_notes.append(f"MEDICATION NOT RECOGNISED (check source, add manually if prescribed): \"{text}\""
                            if color == "RED" else f"Unrecognised medication mention \"{text}\" — {disp.split('→ ', 1)[1]}")
    # vitals that were extracted but have no box on the note (e.g. heart rate) are listed, not dropped;
    # a clinician-entered list (editor field) replaces the extracted one
    if isinstance(ents.get("other_vitals_human"), list):
        other_vitals = [str(x) for x in ents["other_vitals_human"] if str(x).strip()]
    else:
        other_vitals = [str(v.get("text", "")) for v in ents.get("vitals", []) or []
                        if isinstance(v, dict) and re.search(r"\d", str(v.get("text", "")))
                        and _parse_vital_text(str(v.get("text", ""))) is None and not v.get("ai_suggested")]
    seen_sym, other_sym = set(), []
    for s in ents.get("symptoms", []) or []:
        if not isinstance(s, dict) or s.get("negated"):
            continue
        k = DEDUP.get(str(s.get("text", "")).lower(), str(s.get("text", "")).lower())
        if k and k not in seen_sym:
            seen_sym.add(k)
            other_sym.append(k)
    cc_key = next((x["text"] for x in slots if x["key"] == "cc_blank"), "")
    return {"slots": slots, "denied": denied, "proof_lines": proof[:2],
            "multiple_vital_readings": multi,
            "hpi_bg": full[:300], "coords_version": coords["coords_version"],
            "drug_rows": drug_rows, "review_notes": review_notes,
            "denied_allergies": [a.get("text", "") for a in av["denied"]],
            "denied_symptoms": [x.get("text", "") for x in ents.get("symptoms", []) or []
                                if isinstance(x, dict) and x.get("negated")],
            "symptoms_other": [x for x in other_sym if x != cc_key],
            "unrecognised_meds": unrec, "unrecognised_items": unrec_items, "other_vitals": other_vitals}
