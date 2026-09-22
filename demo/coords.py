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
    """Returns (display_text, font_pt, truncated_flag). Never exceeds box."""
    text = "" if text is None else str(text)
    if overflow == "shrink" and len(text) > max_chars and len(text) > 0:
        font_pt = max(6, round(9 * max_chars / len(text), 1))
    if len(text) > max_chars and overflow in ("truncate", "shrink"):
        return text[: max(0, max_chars - 1)] + "…", font_pt, True
    return text, font_pt, len(text) > max_chars


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


def resolve_slots(entities, transcript, template_id="er_discharge"):
    coords = load_coords(template_id)
    S = coords["slots"]
    ents = entities or {}
    slots, denied, proof = [], [], []

    def slot(key, text, color="YELLOW", confidence="", note="", chip=False):
        c = dict(S[key])
        disp, fpt, trunc = fit_text(text, c["max_chars"], c["overflow"])
        is_nil = disp.startswith("NIL")
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

    # chief complaint: first non-negated symptom, deduped
    seen = set()
    for s in ents.get("symptoms", []):
        if s.get("negated"):
            continue
        key = DEDUP.get(s.get("text", "").lower(), s.get("text", "").lower())
        if key in seen:
            continue
        seen.add(key)
        slots.append(slot("cc_blank", key, s.get("color", "YELLOW"), s.get("confidence", "")))
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

    # drug rows (max 4, stacked)
    active = [d for d in ents.get("drugs", []) if not d.get("negated")][:4]
    if not active:
        slots.append(slot("drug_0_name", "NIL — no drug heard", "RED", "", chip=True))
        for key in ("drug_0_dose", "drug_0_unit", "drug_0_freq", "drug_0_duration"):
            if key in S:
                slots.append(slot(key, NIL_MISSING, "RED", ""))
    for i, d in enumerate(active):
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
            s = slot(key, vals[col], cell_color, d.get("confidence", ""),
                     note=d.get("note", "") if col == "name" else "",
                     chip=(col == "duration" or vals[col] == NIL_MISSING))
            slots.append(s)

    # vitals boxes (parse normalized text first, raw fallback)
    vtext = transcript.get("normalized_en") or full
    boxes = {"sys": "", "dia": "", "temp": "", "spo2": ""}
    
    # Structured vitals win whenever present: LLM gap-fills them without
    # conflicting with regex digits (see merge_primary), and human corrections
    # in the editable form write here directly.
    if ents.get("structured_vitals"):
        sv = ents.get("structured_vitals", {})
        boxes["sys"] = sv.get("sys", "")
        boxes["dia"] = sv.get("dia", "")
        boxes["temp"] = sv.get("temp", "")
        boxes["spo2"] = sv.get("spo2", "")
    else:
        # Regex fallback - parse from text (decimal-aware, 'of' separator, long-form BP).
        # Always extract the numeric reading, never the whole "temp is currently ..." sentence.
        _TEMP_RE = r"\d{2,3}(?:\.\d{1,2})?\s*(?:degrees?(?:\s*(?:fahrenheit|celsius|F|C))?|°\s*[FC]?)"
        for v in ents.get("vitals", []):
            t = v.get("text", "")
            m = re.search(r"(\d{2,3}(?:\.\d{1,2})?)\s*(by|/|over|of)\s*(\d{2,3}(?:\.\d{1,2})?)", t)
            if m:
                boxes["sys"], boxes["dia"] = m.group(1), m.group(3)
            elif re.search(r"degree|temp|fahrenheit|°", t, re.I):
                mt = re.search(_TEMP_RE, t, re.I)
                boxes["temp"] = (mt.group(0) if mt else t)[:20]
            elif re.search(r"spo2|o2|%|oxygen", t, re.I):
                ms = re.search(r"\d{2,3}(?:\.\d{1,2})?\s*%?", t)
                boxes["spo2"] = (ms.group(0) if ms else t)[:20]
        if not any(boxes.values()):
            m = re.search(r"(\d{2,3}(?:\.\d{1,2})?)\s*(by|/|over|of)\s*(\d{2,3}(?:\.\d{1,2})?)", vtext)
            if m:
                boxes["sys"], boxes["dia"] = m.group(1), m.group(3)
            else:
                mt = re.search(r"\b" + _TEMP_RE, vtext, re.I)
                if mt:
                    boxes["temp"] = mt.group(0)[:20]
    vmap = {"sys": "vitals_bp_sys", "dia": "vitals_bp_dia", "temp": "vitals_temp", "spo2": "vitals_spo2"}
    for k, key in vmap.items():
        slots.append(slot(key, boxes[k] or NIL_MISSING, "YELLOW" if boxes[k] else "RED", ""))

    # allergies: active line + grey DENIED floats
    act = "; ".join(a.get("text", "") for a in ents.get("allergies", []) if not a.get("negated"))
    slots.append(slot("allergy_line", act or "NIL — none extracted", "YELLOW" if act else "GREEN", ""))
    dy = 154.0
    for a in [x for x in ents.get("allergies", []) if x.get("negated")]:
        denied.append({"x": 30, "y": round(dy, 1), "w": 150, "text": f"{a.get('text','')}"[:40]})
        dy += 2.8
    for s in [x for x in ents.get("symptoms", []) if x.get("negated")]:
        denied.append({"x": 30, "y": round(dy, 1), "w": 150, "text": f"{s.get('text','')}"[:40]})
        dy += 2.8

    # diagnosis + follow-up + date/sign
    dx = ents.get("diagnosis") or {}
    fu = ents.get("followup") or {}
    slots.append(slot("dx_text", dx.get("text") or "NIL — [DR CONFIRM]", "YELLOW" if dx.get("text") else "RED", ""))
    slots.append(slot("dx_code_box", dx.get("icd10") or "NIL", "YELLOW" if dx.get("icd10") else "RED", ""))
    dx_fu = fu.get("text", "")
    if "fu_text" in S:
        slots.append(slot("fu_text", dx_fu or "NIL — no follow-up heard",
                           "YELLOW" if dx_fu else "GREEN", ""))
    slots.append(slot("date_box", __import__("datetime").date.today().isoformat(), "GREEN", ""))
    slots.append(slot("sign_line", ents.get("signature") or "________________", "GREEN", ""))

    # proof micro-strip: max 2 lines, single page strictly
    segs = transcript.get("segments", [])
    span = f"seg {segs[0].get('id', 0)} {segs[0].get('start', 0)}-{segs[0].get('end', 0)}s" if segs else ""
    heard = (active[0].get("source_sentence", "") if active else full)[:130]
    proof.append(f"Heard: \"{heard}\" [{span}]")
    misc = []
    if not any(boxes.values()):
        m = re.search(r"BPA?[^.,]{0,30}", full, re.I)
        if m:
            misc.append(f"unmapped vitals: \"{m.group(0).strip()}\"")
    misc += [f"DENIED: {d['text']}" for d in denied]
    if dx_fu:
        misc.append(f"Follow-up: {dx_fu}"[:60])
    if misc:
        proof.append("; ".join(misc)[:180])

    return {"slots": slots, "denied": denied[:3], "proof_lines": proof[:2],
            "hpi_bg": full[:300], "coords_version": coords["coords_version"]}
