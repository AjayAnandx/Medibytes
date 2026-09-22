"""Stage 6 demo: fill_template(entities, transcript) -> HTML + DOCX bytes.

Demo-scoped from PIPELINE_ARCHITECTURE_REPORT.md Sec 6.
No FerroTERM/ICD lookup, no 99% gate, no DB. Missing -> ___ + RED badge (no block).
Negated -> excluded from active, shown grey DENIED.
"""
import datetime
import io
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))

DEDUP = {"khansi": "cough", "bukhar": "fever"}


def _seg_of(source_sentence, segments):
    for s in segments or []:
        if s.get("text", "").strip() == (source_sentence or "").strip():
            return s.get("id"), f"{s.get('start')}-{s.get('end')}s"
        if source_sentence and source_sentence[:30] in s.get("text", ""):
            return s.get("id"), f"{s.get('start')}-{s.get('end')}s"
    return None, ""


def fill_template(entities_json, transcript_json, template_id="er_discharge"):
    from jinja2 import FileSystemLoader, Environment

    tpl_dir = os.path.join(_HERE, "templates")
    with open(os.path.join(tpl_dir, f"{template_id}.json"), encoding="utf-8") as f:
        spec = json.load(f)

    # Premium path: coordinate slots (single A4 strictly). Falls back to classic below.
    try:
        from coords import resolve_slots
        if os.path.isfile(os.path.join(tpl_dir, f"{template_id}.coords.json")):
            return _fill_premium(spec, entities_json, transcript_json, template_id)
    except Exception as e:
        import sys
        print(f"premium fill failed ({e}); classic fallback", file=sys.stderr)

    env = Environment(loader=FileSystemLoader(tpl_dir))
    tpl = env.get_template(spec["layout"])

    ents = entities_json or {}
    segs = (transcript_json or {}).get("segments", [])

    # chief complaint: first non-negated symptom, deduped
    seen, chief = set(), {"text": "NIL — not heard", "missing": True, "color": "RED", "confidence": ""}
    for s in ents.get("symptoms", []):
        if s.get("negated"):
            continue
        key = DEDUP.get(s.get("text", "").lower(), s.get("text", "").lower())
        if key in seen:
            continue
        seen.add(key)
        sid, span = _seg_of(s.get("source_sentence"), segs)
        chief = {"text": f"{key} ({s.get('source_sentence','')[:80]})",
                 "color": s.get("color", "YELLOW"), "confidence": s.get("confidence", ""),
                 "proof": s.get("source_sentence", ""), "seg": sid}
        break

    drugs = []
    for d in ents.get("drugs", []):
        if d.get("negated"):
            continue
        dose = d.get("dose")
        dose = "NIL" if dose is None or dose == "" else (str(int(float(dose))) if str(dose).replace(".", "", 1).isdigit() and float(dose).is_integer() else str(dose))
        unit = d.get("unit", "") or "NIL"
        _, span = _seg_of(d.get("source_sentence"), segs)
        drugs.append({"name": d.get("name", "") or "NIL", "dose": dose,
                      "unit": unit, "frequency": d.get("frequency", "") or "NIL",
                      "duration": d.get("duration", "") or "NIL", "confidence": d.get("confidence", ""),
                      "color": d.get("color", "YELLOW"), "proof": f"seg {span} {d.get('source_sentence','')[:60]}",
                      "note": d.get("note", "")})

    vitals = ""
    if ents.get("vitals"):
        vitals = "; ".join(v.get("text", "") for v in ents["vitals"])

    # unmapped heard speech: BP-like text in transcript but no vitals extracted
    unmapped = ""
    if not vitals:
        import re
        m = re.search(r"BPA?[^.,]{0,30}", transcript_json.get("text", ""), re.I)
        if m:
            unmapped = m.group(0).strip()

    allergies_active = "; ".join(a.get("text", "") for a in ents.get("allergies", []) if not a.get("negated"))
    allergies_denied = [{"text": a.get("text", ""), "proof": a.get("source_sentence", "")}
                        for a in ents.get("allergies", []) if a.get("negated")]
    symptoms_denied = [{"text": s.get("text", ""), "proof": s.get("source_sentence", "")}
                       for s in ents.get("symptoms", []) if s.get("negated")]

    ctx = {"job_id": ents.get("job_id", transcript_json.get("job_id", "")),
           "language": transcript_json.get("language", ""),
           "template_id": spec["id"], "template_version": spec["version"],
           "date": datetime.date.today().isoformat(),
           "chief_complaint": chief, "hpi": transcript_json.get("text", "")[:500],
           "drugs": drugs, "vitals": vitals, "unmapped": unmapped,
           "allergies_active": allergies_active, "allergies_denied": allergies_denied,
           "symptoms_denied": symptoms_denied}
    html = tpl.render(**ctx)

    # DOCX fallback mirror (minimal styling)
    docx_bytes = None
    try:
        from docx import Document
        doc = Document()
        doc.add_heading(f"ER Discharge (DEMO) - {ctx['job_id']}", 1)
        doc.add_paragraph(f"lang: {ctx['language']} | template: {ctx['template_id']}:{ctx['template_version']} | {ctx['date']}")
        doc.add_heading("Chief Complaint", 2)
        doc.add_paragraph(chief.get("text", "___"))
        doc.add_heading("History", 2)
        doc.add_paragraph(ctx["hpi"])
        doc.add_heading("Drugs", 2)
        if drugs:
            t = doc.add_table(rows=1, cols=5)
            t.style = "Table Grid"
            for i, h in enumerate(["Name", "Dose", "Freq", "Days", "Conf"]):
                t.rows[0].cells[i].text = h
            for d in drugs:
                row = t.add_row().cells
                row[0].text = str(d["name"])
                row[1].text = f"{d['dose']} {d['unit']}"
                row[2].text = str(d["frequency"])
                row[3].text = str(d["duration"])
                row[4].text = f"{d['color']} {d['confidence']}"
        else:
            doc.add_paragraph("NIL — no drug heard")
        doc.add_heading("Vitals", 2)
        doc.add_paragraph(vitals or "NIL")
        doc.add_heading("Allergies (active)", 2)
        doc.add_paragraph(allergies_active or "None extracted.")
        for a in allergies_denied:
            doc.add_paragraph(f"DENIED: {a['text']} - {a['proof']}")
        doc.add_heading("Diagnosis (ICD-10)", 2)
        doc.add_paragraph("NIL — [PHYSICIAN TO CONFIRM]")
        doc.add_paragraph("Demo - not for clinical use.")
        buf = io.BytesIO()
        doc.save(buf)
        docx_bytes = buf.getvalue()
    except Exception:
        docx_bytes = None

    return {"html": html, "docx_bytes": docx_bytes, "preview_data": ctx}


def _fill_premium(spec, entities_json, transcript_json, template_id):
    """Premium single-A4 render: absolute mm slots + word-only editable DOCX."""
    from jinja2 import FileSystemLoader, Environment
    from coords import resolve_slots

    tpl_dir = os.path.join(_HERE, "templates")
    env = Environment(loader=FileSystemLoader(tpl_dir))
    tpl = env.get_template(spec["layout"])
    r = resolve_slots(entities_json, transcript_json, template_id)
    ctx = {"job_id": (entities_json or {}).get("job_id", (transcript_json or {}).get("job_id", "")),
           "language": (transcript_json or {}).get("language", ""),
           "template_id": spec["id"], "template_version": spec["version"],
           "date": datetime.date.today().isoformat(),
           "slots": r["slots"], "denied": r["denied"],
           "proof_lines": r["proof_lines"], "hpi_bg": r["hpi_bg"]}
    html = tpl.render(**ctx)
    docx_bytes = _docx_word_editable(ctx)
    return {"html": html, "docx_bytes": docx_bytes, "preview_data": ctx}


def _docx_word_editable(ctx):
    """DOCX draft: grey locked chrome + YELLOW editable extracted-word runs only."""
    try:
        from docx import Document
        from docx.shared import Pt, RGBColor
        from docx.enum.text import WD_COLOR_INDEX
    except Exception:
        return None
    grey = RGBColor(0x6B, 0x72, 0x80)
    doc = Document()
    doc.add_heading(f"ER DISCHARGE (DRAFT) - {ctx['job_id']}", 1)
    lock = doc.add_paragraph()
    lr = lock.add_run("Grey = locked template chrome. Yellow = extracted words (editable). "
                      "Edit ONLY yellow runs.")
    lr.font.color.rgb = grey
    lr.font.size = Pt(8)
    for s in ctx["slots"]:
        p = doc.add_paragraph()
        lab = p.add_run(f"[{s['key']}] ")
        lab.font.color.rgb = grey
        lab.font.size = Pt(8)
        txt = str(s.get("text", ""))
        w = p.add_run(txt)
        if txt.startswith("NIL") or txt == "________________":
            w.font.color.rgb = grey  # locked placeholder, not editable
            w.font.size = Pt(8)
        else:
            w.font.highlight_color = WD_COLOR_INDEX.YELLOW
            w.font.size = Pt(9)
        if s.get("note"):
            n = p.add_run(f"  ({s['note']})")
            n.font.color.rgb = grey
            n.font.size = Pt(7)
    for d in ctx.get("denied", []):
        p = doc.add_paragraph()
        r = p.add_run(f"DENIED (locked): {d.get('text','')}")
        r.font.color.rgb = grey
    foot = doc.add_paragraph("Demo draft — single A4 record is the HTML print. Verify before clinical use.")
    foot.runs[0].font.color.rgb = grey
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
