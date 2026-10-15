"""MediBytes product demo UI: mic/file -> clean -> text -> entities -> discharge.
Run via one command:  python run_demo.py   (installs deps, warms cache, launches this UI)
Direct:  streamlit run demo/app.py
"""
import copy
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile
import subprocess
import shutil
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import streamlit as st

try:
    import matplotlib.pyplot as plt
    HAS_PLOT = True
except Exception:
    HAS_PLOT = False

from audio_clean import _load_mono_float, clean_audio
from stt_extract import TranscriptionError, run_stt_extract, run_text_extract
from fill_template import TemplateRenderError, fill_template
from ocr_extract import SUPPORTED_EXTS as OCR_EXTS, extract_text_from_image  # easyocr loads lazily

COLOR = {"GREEN": "#10B981", "YELLOW": "#F59E0B", "RED": "#EF4444", "DENIED": "#9CA3AF"}

NARRATION = [
    ("0 Receive", "Doctor taps record or drops a file. Size/duration guard + ticket ID, no duplicates."),
    ("1 Clean", "16k mono + VAD silence skip + hiss reduction. Compare before/after waveform + audio."),
    ("2 Speech-to-text", "Offline on this laptop (tiny/base/small). Every word timestamped."),
    ("3 Normalize", "Hinglish fixed: bukhar->fever, BID->twice daily. Clean English for hospital systems."),
    ("4 Extract", "Drugs/symptoms/vitals/allergies with proof sentence + confidence. Negated items excluded."),
    ("5 Discharge note", "Words sit in the ER template. Download HTML/DOCX or Print->PDF. Demo only, verify first."),
]

CACHED = ["demo-001 fever (mock)", "demo-002 cough (mock)", "luvvoice-001 real MP3 (base)"]

OLLAMA_MODELS = ["llama3.2:3b", "qwen2.5:0.5b"]


def _ollama_available(model):
    """Check if Ollama binary exists and model is pulled."""
    if not shutil.which("ollama"):
        return False, "ollama not installed"
    try:
        out = subprocess.run(["ollama", "list"], capture_output=True,
                             encoding="utf-8", errors="ignore", timeout=10)
        if model in (out.stdout or ""):
            return True, ""
        return False, f"model {model} not pulled"
    except Exception as e:
        return False, type(e).__name__


def _job_from_label(label):
    return label.split()[0]


def _load_cached(job):
    tj = ej = meta = html = None
    for p, key in ((f"transcripts/{job}.json", "tj"), (f"entities/{job}.entities.json", "ej"),
                   (f"cleaned/{job}.meta.json", "meta"), (f"exports/{job}.html", "html")):
        fp = os.path.join(_HERE, p)
        if os.path.isfile(fp):
            with open(fp, encoding="utf-8") as f:
                val = f.read() if key == "html" else json.load(f)
            if key == "tj":
                tj = val
            elif key == "ej":
                ej = val
            elif key == "meta":
                meta = val
            else:
                html = val
    return tj, ej, meta, html


def _wavefig(y0, sr0, y1, sr1, vad):
    fig, ax = plt.subplots(1, 2, figsize=(10, 2.6))
    ax[0].plot(y0[max(0, len(y0) - sr0 * 10):])
    ax[0].set_title("raw")
    ax[1].plot(y1[max(0, len(y1) - sr1 * 10):])
    ax[1].set_title(f"cleaned vad={vad}")
    for a in ax:
        a.set_xlabel("samples (last 10s)")
    fig.tight_layout()
    return fig


def _entity_cards(ej):
    for grp in ("drugs", "symptoms", "vitals", "allergies"):
        items = ej.get(grp, []) if ej else []
        if not items:
            continue
        st.markdown(f"**{grp.capitalize()} ({len(items)})**")
        for item in items:
            if not isinstance(item, dict):
                item = {"text": str(item), "ai_suggested": True}
            c = item.get("color", "YELLOW")
            neg = " [NEGATED]" if item.get("negated") else ""
            # AI provenance: unreviewed AI values are never shown as GREEN
            ai_f = ["all fields"] if item.get("ai_suggested") else list(item.get("ai_fields") or [])
            if ai_f and c == "GREEN":
                c = "YELLOW"
            ai_txt = f"<br><b>AI-suggested - verify:</b> {', '.join(ai_f)}" if ai_f else ""
            st.markdown(
                f"<div style='border-left:6px solid {COLOR.get(c, '#999')};padding:8px;margin:6px 0'>"
                f"<b>{grp[:-1]} {c}</b>{neg} - {item.get('name') or item.get('text')} "
                f"{item.get('dose', '')} {item.get('unit', '')} {item.get('frequency', '')} "
                f"{item.get('duration', '')} ({item.get('confidence', '?')})<br>"
                f"<i>Heard:</i> {item.get('source_sentence', '')}{ai_txt}"
                f"{('<br><small>' + item.get('note', '') + '</small>') if item.get('note') else ''}</div>",
                unsafe_allow_html=True,
            )
    if ej and ej.get("negations"):
        with st.expander("Negations (rule stand-in for CAN-BERT)"):
            st.json(ej["negations"])


# ---- Editable discharge: slot-keyed field boxes over the AI entities ----
# Slot keys come from templates/er_discharge.coords.json via coords.resolve_slots.
# Edits patch ENTITY fields (not slot text), then slots re-resolve so colors,
# NIL/RED states and truncation recompute. transcripts/ stay immutable.
_EDITABLE_SLOTS = (
    "patient_name",  # image source only (coords.resolve_slots adds it only for images)
    "cc_blank",
    "drug_0_name", "drug_0_dose", "drug_0_unit", "drug_0_freq", "drug_0_duration",
    "drug_1_name", "drug_1_dose", "drug_1_unit", "drug_1_freq", "drug_1_duration",
    "vitals_bp_sys", "vitals_bp_dia", "vitals_temp", "vitals_spo2",
    "allergy_line", "dx_text", "dx_code_box", "fu_text", "sign_line",
)
_SLOT_LABELS = {
    "patient_name": "Patient name",
    "cc_blank": "Chief complaint",
    "drug_0_name": "Drug 1 name", "drug_0_dose": "Drug 1 dose", "drug_0_unit": "Drug 1 unit",
    "drug_0_freq": "Drug 1 frequency", "drug_0_duration": "Drug 1 duration",
    "drug_1_name": "Drug 2 name", "drug_1_dose": "Drug 2 dose", "drug_1_unit": "Drug 2 unit",
    "drug_1_freq": "Drug 2 frequency", "drug_1_duration": "Drug 2 duration",
    "vitals_bp_sys": "BP systolic", "vitals_bp_dia": "BP diastolic",
    "vitals_temp": "Temperature", "vitals_spo2": "SpO2",
    "allergy_line": "Allergies (active, ;-separated)",
    "dx_text": "Diagnosis", "dx_code_box": "ICD-10 code",
    "fu_text": "Follow-up", "sign_line": "Signature",
}


def _slot_defaults(ej, tj):
    """Current slot_key -> text from the AI entities (form defaults)."""
    try:
        from coords import resolve_slots
        r = resolve_slots(ej, tj, "er_discharge")
        return {s["key"]: str(s.get("text", "")) for s in r["slots"]}, r
    except Exception as e:
        st.warning(f"slot resolve failed: {e}")
        return {}, {"slots": [], "denied": [], "proof_lines": []}


def _multi_readings_warning(resolved, image=False):
    """List every different reading of a vital the compact box cannot show."""
    labels = {"bp": "BP", "temp": "temperature", "spo2": "SpO2"}
    where = "OCR line" if image else "heard"
    for kind, rs in (resolved.get("multiple_vital_readings") or {}).items():
        lines = [f"- **{r['value']}**" + (f" ×{r['count']}" if r.get("count", 1) > 1 else "")
                 + "".join(f" — {where}: \"{s}\"" for s in r.get("sources", []))
                 for r in rs]
        st.warning(f"Multiple {labels.get(kind, kind)} readings — verify. No reading was chosen "
                   f"automatically; the note box shows the last one mentioned. Enter the correct "
                   f"value(s) in the form to resolve.\n\n" + "\n".join(lines))


def _parse_dose(raw):
    raw = (raw or "").strip()
    if raw.upper().startswith("NIL") or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return raw  # keep physician text; coords renders it verbatim


def _apply_edits(orig_ej, edits, defaults=None):
    """Patch a deepcopy of the AI entities from slot_key -> new text.

    defaults: slot_key -> currently resolved AI text (from _slot_defaults). Used
    only to keep untouched vitals when another vital is corrected.
    Returns (patched_entities, audit_entries). Never mutates transcripts.
    """
    patched = copy.deepcopy(orig_ej)
    audit = []
    ts = datetime.datetime.now().isoformat(timespec="seconds")

    def _record(slot_key, path, old, new):
        if str(old) != str(new):
            audit.append({"ts": ts, "slot_key": slot_key, "entity_path": path,
                          "original_ai": old, "human_corrected": new})

    # patient name (image source only) -> patient.name; NIL/blank keeps it missing (RED)
    if "patient_name" in edits:
        new = edits["patient_name"].strip()
        if new.upper().startswith("NIL"):
            new = ""
        pt = patched.setdefault("patient", {})
        _record("patient_name", "patient.name", pt.get("name", ""), new)
        pt["name"] = new
        pt["color"] = "YELLOW"
        pt.pop("ai_suggested", None)  # clinician-entered, no longer an AI suggestion

    # chief complaint -> first non-negated symptom
    if "cc_blank" in edits:
        new = edits["cc_blank"].strip()
        tgt = next((s for s in patched.get("symptoms", []) if not s.get("negated")), None)
        if tgt is None:
            tgt = {"text": "", "confidence": 0.85, "color": "YELLOW",
                   "source_sentence": "", "negated": False, "note": "human-added"}
            patched.setdefault("symptoms", []).append(tgt)
        _record("cc_blank", "symptoms[0].text", tgt.get("text", ""), new)
        tgt["text"] = new
        tgt["color"] = "YELLOW"
        tgt.pop("ai_suggested", None)

    # drug rows -> active (non-negated) drugs, cap 4 (coords rows)
    active = [d for d in patched.get("drugs", []) if not d.get("negated")]
    for i in (0, 1):
        cols = {"name": f"drug_{i}_name", "dose": f"drug_{i}_dose", "unit": f"drug_{i}_unit",
                "freq": f"drug_{i}_freq", "duration": f"drug_{i}_duration"}
        if not any(k in edits for k in cols.values()):
            continue
        while len(active) <= i:
            active.append({"name": "", "dose": None, "unit": "", "frequency": "",
                           "duration": "", "confidence": 0.85, "color": "YELLOW",
                           "source_sentence": "", "negated": False, "note": "human-added"})
            patched.setdefault("drugs", []).append(active[-1])
        row = active[i]
        fmap = {"name": "name", "dose": "dose", "unit": "unit",
                "freq": "frequency", "duration": "duration"}
        for col, field in fmap.items():
            k = cols[col]
            if k not in edits:
                continue
            new = edits[k].strip()
            old = row.get(field, "")
            if col == "dose":
                new_v = _parse_dose(new)
                _record(k, f"drugs[{i}].dose", old, new_v)
                row[field] = new_v
            else:
                _record(k, f"drugs[{i}].{field}", old, new)
                row[field] = new
        # provenance: only the fields the clinician edited stop being AI suggestions
        edited = [field for col, field in fmap.items() if cols[col] in edits]
        if row.pop("ai_suggested", False):
            row["ai_fields"] = [f for f in fmap.values() if f not in edited]
        elif row.get("ai_fields"):
            row["ai_fields"] = [f for f in row["ai_fields"] if f not in edited]
        row["color"] = "YELLOW"
        row["confidence"] = max(float(row.get("confidence", 0) or 0), 0.85)

    # vitals -> structured_vitals (authoritative per field for coords). Untouched
    # vitals are not copied in: coords keeps their regex/AI value per field, and
    # any regex-vs-AI conflict note stays until a human sets that field.
    sv = patched.setdefault("structured_vitals", {})
    vmap = {"vitals_bp_sys": "sys", "vitals_bp_dia": "dia",
            "vitals_temp": "temp", "vitals_spo2": "spo2"}
    for sk, field in vmap.items():
        if sk in edits:
            new = edits[sk].strip()
            if new.upper().startswith("NIL"):
                new = ""
            _record(sk, f"structured_vitals.{field}", sv.get(field, ""), new)
            sv[field] = new
            if field in (patched.get("structured_vitals_ai") or []):
                patched["structured_vitals_ai"] = [f for f in patched["structured_vitals_ai"] if f != field]

    # allergies active line -> ;-separated replace (negated DENIED stay locked).
    # The clinician's entry is authoritative (allergy_status): "No known
    # allergies"/NKDA/None is a status, never an allergen; placeholders are ignored.
    if "allergy_line" in edits:
        new = edits["allergy_line"].strip()
        old_active = "; ".join(a.get("text", "") for a in patched.get("allergies", [])
                               if isinstance(a, dict) and not a.get("negated"))
        _record("allergy_line", "allergies.active", old_active, new)
        patched["allergies"] = [a for a in patched.get("allergies", [])
                                if isinstance(a, dict) and a.get("negated")]
        if re.match(r"^\s*(?:no\s+known\s+(?:drug\s+)?allerg(?:y|ies)|nkda|nka|none(?:\s+known)?|nil)\b",
                    new, re.I) and not new.upper().startswith("NIL —"):
            patched["allergy_status"] = "none_known"
        else:
            patched["allergy_status"] = "reviewed"
            for part in [p.strip() for p in new.split(";") if p.strip()
                         and not p.strip().upper().startswith(("NIL", "NOT DOCUMENTED"))]:
                patched["allergies"].append({"text": part, "negated": False, "confidence": 0.9,
                                             "color": "YELLOW", "source_sentence": "",
                                             "note": "human-corrected"})

    # diagnosis + follow-up + signature
    dx = patched.setdefault("diagnosis", {})
    if "dx_text" in edits:
        _record("dx_text", "diagnosis.text", dx.get("text", ""), edits["dx_text"].strip())
        dx["text"] = edits["dx_text"].strip()
        dx["color"] = "YELLOW"
        dx.pop("ai_suggested", None)
    if "dx_code_box" in edits:
        _record("dx_code_box", "diagnosis.icd10", dx.get("icd10", ""), edits["dx_code_box"].strip())
        dx["icd10"] = edits["dx_code_box"].strip()
    fu = patched.setdefault("followup", {})
    if "fu_text" in edits:
        new = edits["fu_text"].strip()
        if new.upper().startswith("NIL"):
            new = ""
        _record("fu_text", "followup.text", fu.get("text", ""), new)
        fu["text"] = new
        fu.pop("ai_suggested", None)
    if "sign_line" in edits:
        _record("sign_line", "signature", patched.get("signature", ""), edits["sign_line"].strip())
        patched["signature"] = edits["sign_line"].strip()

    return patched, audit


def _persist_corrected(job_id, patched, audit_entries):
    """Write corrected entities + re-rendered HTML/DOCX + audit trail. Returns paths."""
    from fill_template import fill_template as _fill
    # merge audit with any existing trail (never lose history)
    afp = os.path.join(_HERE, "entities", f"{job_id}.audit.json")
    trail = []
    try:
        if os.path.isfile(afp):
            with open(afp, encoding="utf-8") as f:
                trail = json.load(f)
    except Exception:
        trail = []
    trail.extend(audit_entries)
    efp = os.path.join(_HERE, "entities", f"{job_id}.corrected.entities.json")
    with open(efp, "w", encoding="utf-8") as f:
        json.dump(patched, f, indent=2, ensure_ascii=False)
    with open(afp, "w", encoding="utf-8") as f:
        json.dump(trail, f, indent=2, ensure_ascii=False)
    return efp, afp


def _append_audit_events(job_id, events, image=False):
    """Append events (verification history) to the job's audit trail; never rewrites history."""
    if image:  # session-only trail for image jobs
        key = f"{job_id}_audit"
        st.session_state[key] = st.session_state.get(key, []) + list(events)
        return
    afp = os.path.join(_HERE, "entities", f"{job_id}.audit.json")
    trail = []
    try:
        if os.path.isfile(afp):
            with open(afp, encoding="utf-8") as f:
                trail = json.load(f)
    except Exception:
        trail = []
    trail.extend(events)
    os.makedirs(os.path.dirname(afp), exist_ok=True)
    with open(afp, "w", encoding="utf-8") as f:
        json.dump(trail, f, indent=2, ensure_ascii=False)


def _changed_audit(form_vals, saved_edits, ai_defaults, full_audit):
    """Audit entries for boxes the doctor actually changed vs what was on screen.

    Placeholders (NIL, ____) and untouched boxes are not corrections; a box
    set back to the AI value is still a change of the working note.
    """
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    by_slot = {a["slot_key"]: a for a in full_audit}
    out = []
    for k, v in form_vals.items():
        prev = saved_edits.get(k, ai_defaults.get(k, ""))
        if str(v) == str(prev):
            continue
        a = dict(by_slot.get(k) or {"ts": ts, "slot_key": k, "entity_path": "",
                                    "original_ai": ai_defaults.get(k, ""),
                                    "human_corrected": str(v).strip()})
        a["previous_value"] = prev
        if str(v) == str(ai_defaults.get(k, "")):
            a["note"] = "reverted to AI value"
        out.append(a)
    return out


def _note_fingerprint(ents):
    """Content hash of the working note entities (what Verify approves)."""
    blob = json.dumps(ents, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _new_run_id():
    """Identifies one processing/editing run of a job (audit entries carry it)."""
    return f"{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _corrected_export_paths(job_id):
    ex = os.path.join(_HERE, "exports")
    return (os.path.join(ex, f"{job_id}.corrected.html"), os.path.join(ex, f"{job_id}.corrected.docx"),
            os.path.join(ex, f"{job_id}.corrected.verified.json"))


def _supersede_stale_exports(job_id, cur_fp, run_id):
    """Keep exports/{job}.corrected.* at the canonical path only while their
    manifest matches the current note; otherwise move them (never delete) to
    a .corrected.superseded-<ts>-<fp>.* archive name and audit the move."""
    html_p, docx_p, man_p = _corrected_export_paths(job_id)
    present = [p for p in (html_p, docx_p, man_p) if os.path.isfile(p)]
    if not present:
        return
    man = {}
    try:
        with open(man_p, encoding="utf-8") as f:
            man = json.load(f)
    except Exception:
        man = {}
    if man.get("status") == "verified" and man.get("note_fingerprint") == cur_fp:
        return  # still exactly the current verified note
    tag = f"{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}-{man.get('note_fingerprint') or 'unverified'}"
    archived = []
    for p in present:
        stem = os.path.basename(p).split(".corrected.", 1)[1]  # html | docx | verified.json
        base = os.path.join(os.path.dirname(p), f"{job_id}.corrected.superseded-{tag}")
        dst, n = f"{base}.{stem}", 1
        while os.path.exists(dst):
            dst, n = f"{base}-{n}.{stem}", n + 1
        os.replace(p, dst)
        archived.append({"from": os.path.basename(p), "to": os.path.basename(dst)})
    _append_audit_events(job_id, [{
        "ts": datetime.datetime.now().isoformat(timespec="seconds"), "run_id": run_id,
        "event": "corrected_exports_superseded",
        "reason": "exports no longer match the current verified note",
        "export_fingerprint": man.get("note_fingerprint"), "current_fingerprint": cur_fp,
        "archived": archived}])


def _reset_note_state(job_id):
    """New processing job: drop editor/verification state (audit history is kept)."""
    for k in (f"{job_id}_orig", f"{job_id}_edits", f"{job_id}_verified",
              f"{job_id}_verified_fp", f"{job_id}_needs_reverify", f"{job_id}_run_id"):
        st.session_state.pop(k, None)
    for sk in _EDITABLE_SLOTS:
        st.session_state.pop(f"{job_id}_{sk}", None)


def _ocr_tab(image_bytes, ocr):
    """Image-source first tab: uploaded image, verbatim OCR text, confidence."""
    ocr = ocr or {}
    lines = ocr.get("lines", [])
    c1, c2 = st.columns([1, 1.2])
    if image_bytes:
        try:
            c1.image(image_bytes, caption=ocr.get("source_file", ""))
        except Exception as e:
            c1.warning(f"Preview unavailable ({type(e).__name__})")
    low = min((ln["min_confidence"] for ln in lines), default=0.0)
    c2.markdown(f"**Raw OCR text** (verbatim) — engine `{ocr.get('engine', '')}`, "
                f"{len(lines)} lines, lowest confidence {low:.2f}")
    c2.code(ocr.get("text", ""), language=None)
    with st.expander("Per-line confidence"):
        st.dataframe([{"line": ln["line"], "min_confidence": ln["min_confidence"],
                       "blocks": ln["block_count"], "text": ln["text"]} for ln in lines],
                     use_container_width=True, hide_index=True)
    with st.expander("Full OCR result (JSON)"):
        st.json(ocr)


def _show_result(job_id, tj, ej, meta, raw_path=None, clean_path=None, prebuilt_html=None,
                 source="audio", image_bytes=None, ocr=None):
    """Result tabs. source="audio" (default) is the original behavior.

    source="image": OCR tab instead of Audio, OCR line numbers instead of
    timestamps, and corrections / audit / verify / exports live in session
    state only (nothing written to transcripts/, entities/ or exports/).
    """
    image = source == "image"
    st.success(f"Job `{job_id}`  |  lang `{tj.get('language')}`  |  engine `{tj.get('stt_engine')}`")
    if not image and str(tj.get("stt_engine", "")).startswith("mock"):
        st.warning("SYNTHETIC transcript (mock STT): this is a built-in demo script, not your audio. "
                   "Do not use for clinical care.")
    if image:
        steps = ["receive ok", f"OCR {len(tj.get('segments', []))} lines",
                 f"{len(ej.get('drugs', []))} drugs", "discharge ready"]
    else:
        steps = ["receive ok", f"clean vad={meta.get('vad_ratio')}" if meta else "clean",
                 f"{len(tj.get('segments', []))} segments", f"{len(ej.get('drugs', []))} drugs",
                 "discharge ready"]
    st.write("  →  ".join(f"✅ {s}" for s in steps))

    tab_audio, tab_text, tab_ent, tab_note = st.tabs(["OCR" if image else "Audio", "Transcript",
                                                      "Entities", "Discharge note"])
    with tab_audio:
        if image:
            _ocr_tab(image_bytes, ocr)
        else:
            c1, c2 = st.columns(2)
            if raw_path and os.path.isfile(raw_path or ""):
                c1.markdown("**Raw**")
                c1.audio(raw_path)
            if clean_path and os.path.isfile(clean_path or ""):
                c2.markdown("**Cleaned 16k mono**")
                c2.audio(clean_path)
            if HAS_PLOT and raw_path and clean_path and os.path.isfile(raw_path) and os.path.isfile(clean_path):
                try:
                    y0, sr0 = _load_mono_float(raw_path)
                    y1, sr1 = _load_mono_float(clean_path)
                    st.pyplot(_wavefig(y0, sr0, y1, sr1, (meta or {}).get("vad_ratio", "?")))
                except Exception as e:
                    st.warning(f"waveform skipped: {e}")
            if meta:
                legacy = "snr_before_db" in meta and "quality_note" not in meta
                keys = ("duration_s", "vad_ratio", "vad_method", "input_rms_dbfs", "input_peak_dbfs",
                        "input_clipped_ratio", "quality_note") + (() if legacy else ("denoise",))
                st.json({k: meta.get(k) for k in keys if k in meta})
                if legacy:
                    st.caption("Audio metadata from an older version: its 'SNR' values were a loudness "
                               "estimate (not a measured signal-to-noise ratio) and its denoise label "
                               "was not verified, so neither is shown. Re-run the file for level data.")
    with tab_text:
        st.code(tj.get("text", ""))
        st.success(tj.get("normalized_en", ""))
        if image:  # OCR lines: no timestamps / word times exist
            with st.expander(f"OCR lines ({len(tj.get('segments', []))})"):
                st.json([{"line": s.get("id"), "text": s.get("text"), "confidence": s.get("confidence"),
                          "lang": s.get("lang")} for s in tj.get("segments", [])])
        else:
            with st.expander(f"Segments + word times ({len(tj.get('segments', []))})"):
                st.json(tj.get("segments", []))
    with tab_ent:
        _entity_cards(ej)
        if image:
            with st.expander("Entities JSON (debug)"):
                st.json(ej)
    with tab_note:
        # Editable discharge: left = field boxes prefilled with AI values,
        # right = re-rendered A4 preview. PDF/print is a snapshot of the
        # VERIFIED corrected note, never the primary surface.
        try:
            orig_key, edits_key, ver_key = f"{job_id}_orig", f"{job_id}_edits", f"{job_id}_verified"
            audit_key = f"{job_id}_audit"  # image source only
            run_key = f"{job_id}_run_id"
            if orig_key not in st.session_state:
                st.session_state[orig_key] = copy.deepcopy(ej)
                st.session_state[edits_key] = {}
                st.session_state[ver_key] = False
                st.session_state[run_key] = _new_run_id()  # new processing/editing run
            run_id = st.session_state.setdefault(run_key, _new_run_id())
            orig_ej = st.session_state[orig_key]
            saved_edits = st.session_state[edits_key]

            ai_defaults, _r0 = _slot_defaults(orig_ej, tj)
            # working entities = AI + saved human edits
            work_ej, _ = _apply_edits(orig_ej, saved_edits, ai_defaults) if saved_edits else (orig_ej, [])

            col_form, col_prev = st.columns([1, 1.35])
            with col_form:
                st.markdown("**Correct the fields** — values prefilled by AI. Transcript (left tabs) is immutable proof.")
                with st.form(f"correct_{job_id}"):
                    form_vals = {}
                    for sk in _EDITABLE_SLOTS:
                        if sk not in ai_defaults:
                            continue
                        cur = saved_edits.get(sk, ai_defaults.get(sk, ""))
                        if sk in ("cc_blank", "allergy_line", "dx_text", "fu_text"):
                            form_vals[sk] = st.text_area(_SLOT_LABELS.get(sk, sk), value=cur, key=f"{job_id}_{sk}")
                        else:
                            form_vals[sk] = st.text_input(_SLOT_LABELS.get(sk, sk), value=cur, key=f"{job_id}_{sk}")
                    submitted = st.form_submit_button("Apply corrections", use_container_width=True)
                if submitted:
                    # keep only real diffs vs AI original; audit only boxes changed vs the
                    # note on screen. Verification follows the content fingerprint below.
                    new_edits = {k: v for k, v in {**saved_edits, **form_vals}.items()
                                 if str(v) != str(ai_defaults.get(k, ""))}
                    patched, full_audit = _apply_edits(orig_ej, new_edits, ai_defaults)
                    new_audit = [{**a, "run_id": run_id}
                                 for a in _changed_audit(form_vals, saved_edits, ai_defaults, full_audit)]
                    st.session_state[edits_key] = new_edits
                    if new_audit:
                        if image:  # session-only audit trail, no disk writes
                            st.session_state[audit_key] = st.session_state.get(audit_key, []) + new_audit
                        else:
                            _persist_corrected(job_id, patched, new_audit)
                        st.success(f"Saved {len(new_audit)} correction(s). Review the preview, then Verify.")
                    else:
                        st.info("No changes.")
                    st.rerun()

                # re-resolve AFTER edits for RED count + preview
                cur_ej, _ = _apply_edits(orig_ej, saved_edits, ai_defaults) if saved_edits else (orig_ej, [])
                _d, r_cur = _slot_defaults(cur_ej, tj)
                _multi_readings_warning(r_cur, image)
                reds = [s for s in r_cur.get("slots", []) if s.get("color") == "RED"]
                # Verification approves exact note content: any later change (edit or
                # revert) invalidates it; reruns / no-op Apply leave content unchanged.
                fp_key, stale_key = f"{job_id}_verified_fp", f"{job_id}_needs_reverify"
                cur_fp = _note_fingerprint(cur_ej)
                if st.session_state[ver_key] and st.session_state.get(fp_key) != cur_fp:
                    st.session_state[ver_key] = False
                    st.session_state[stale_key] = True
                    _append_audit_events(job_id, [{
                        "ts": datetime.datetime.now().isoformat(timespec="seconds"), "run_id": run_id,
                        "event": "verification_invalidated",
                        "reason": "note content changed after verification",
                        "verified_fingerprint": st.session_state.get(fp_key),
                        "current_fingerprint": cur_fp}], image)
                if st.session_state.get(stale_key):
                    st.warning("This note was changed after it was verified — it needs re-verification. "
                               "Corrected exports stay locked until you verify again.")
                if not image:  # disk exports must never outlive the note they verified
                    try:
                        _supersede_stale_exports(job_id, cur_fp, run_id)
                    except Exception as e:
                        st.error(f"Could not archive outdated corrected exports ({e}); "
                                 f"do not use exports/{job_id}.corrected.* until re-verified.")
                trail = []
                if image:
                    trail = st.session_state.get(audit_key, [])
                else:
                    try:
                        afp0 = os.path.join(_HERE, "entities", f"{job_id}.audit.json")
                        if os.path.isfile(afp0):
                            with open(afp0, encoding="utf-8") as f:
                                trail = json.load(f)
                    except Exception:
                        pass
                # only this run's corrections count; earlier runs stay as history
                corr = [e for e in trail if isinstance(e, dict) and e.get("slot_key")]
                n_audit = sum(1 for e in corr if e.get("run_id") == run_id)
                earlier = len(corr) - n_audit
                st.caption(f"Audited corrections: {n_audit} | RED missing: {len(reds)}"
                           + (f" | {earlier} from earlier runs kept in history (not counted)" if earlier else ""))
                can_verify = n_audit > 0 and len(reds) == 0 and not st.session_state[ver_key]
                if st.session_state[ver_key]:
                    st.success("Verified — corrected exports unlocked.")
                elif st.button("Verify & unlock corrected exports",
                               disabled=not can_verify, use_container_width=True,
                               key=f"verify_{job_id}"):
                    st.session_state[ver_key] = True
                    st.session_state[fp_key] = cur_fp
                    st.session_state[stale_key] = False
                    _append_audit_events(job_id, [{
                        "ts": datetime.datetime.now().isoformat(timespec="seconds"), "run_id": run_id,
                        "event": "verified", "note_fingerprint": cur_fp,
                        "audited_corrections": n_audit}], image)
                    st.rerun()
                if not can_verify and not st.session_state[ver_key]:
                    st.caption("Verify requires ≥1 audited correction and zero RED fields (403-gate).")

            with col_prev:
                try:
                    prev = fill_template(cur_ej, tj)
                    html = prev["html"] if isinstance(prev, dict) else prev
                    st.components.v1.html(html, height=620, scrolling=True)
                    st.download_button("Download AI-original HTML", prebuilt_html or html,
                                       file_name=f"{job_id}.ai-original.html", mime="text/html")
                    if st.session_state[ver_key]:
                        st.download_button("Download CORRECTED HTML (verified)", html,
                                           file_name=f"{job_id}.corrected.html", mime="text/html")
                        docx_bytes = prev.get("docx_bytes") if isinstance(prev, dict) else None
                        if docx_bytes:
                            if not image:  # image: in-memory download only
                                efp, dfp, mfp = _corrected_export_paths(job_id)
                                try:
                                    os.makedirs(os.path.dirname(efp), exist_ok=True)
                                    with open(efp, "w", encoding="utf-8") as f:
                                        f.write(html)
                                    with open(dfp, "wb") as f:
                                        f.write(docx_bytes)
                                    # manifest: which exact note these files are (stale check)
                                    with open(mfp, "w", encoding="utf-8") as f:
                                        json.dump({"job_id": job_id, "run_id": run_id, "status": "verified",
                                                   "note_fingerprint": st.session_state.get(fp_key),
                                                   "files": [os.path.basename(efp), os.path.basename(dfp)]},
                                                  f, indent=2)
                                except Exception as e:
                                    st.warning(f"persist skipped: {e}")
                            st.download_button("Download CORRECTED DOCX (verified)", docx_bytes,
                                               file_name=f"{job_id}.corrected.docx")
                    else:
                        st.caption("Corrected exports locked until Verify (no RED + audited). Print/PDF only from the verified note.")
                    if isinstance(prev, dict) and prev.get("docx_error"):
                        st.warning(f"DOCX could not be generated ({prev['docx_error']}); HTML note is complete.")
                except TemplateRenderError as e:
                    st.error(f"Discharge note could not be generated — no preview, download or export "
                             f"is available until this is fixed.\n\n{e}")
                except Exception as e:
                    st.warning(f"preview skipped: {e}")
            with st.expander("Show Source links"):
                for s in tj.get("segments", []):
                    if image:
                        st.write(f"OCR line {s.get('id')} : {s.get('text')}")
                    else:
                        st.write(f"seg {s.get('id')} [{s.get('start')}-{s.get('end')}s] : {s.get('text')}")
            with st.expander("Audit trail (AI vs human)"):
                if image:
                    if st.session_state.get(audit_key):
                        st.json(st.session_state[audit_key])
                    else:
                        st.caption("No corrections audited yet (kept in this session only).")
                else:
                    try:
                        afp1 = os.path.join(_HERE, "entities", f"{job_id}.audit.json")
                        if os.path.isfile(afp1):
                            with open(afp1, encoding="utf-8") as f:
                                st.json(json.load(f))
                        else:
                            st.caption("No corrections audited yet.")
                    except Exception as e:
                        st.warning(f"audit read failed: {e}")
        except Exception as e:
            st.warning(f"preview skipped: {e}")


def _ocr_segments(ocr):
    """OCR result lines -> transcript-compatible segments (verbatim line text).

    No timestamps exist for images, so start/end are 0.0; lang is refreshed
    by run_text_extract. Nothing is added beyond what EasyOCR returned.
    """
    return [{"id": ln["line"], "text": ln["text"], "start": 0.0, "end": 0.0,
             "lang": "", "confidence": ln["min_confidence"], "words": []}
            for ln in ocr.get("lines", [])]


def _image_job_id(upload_name, content):
    """img-<safe-stem>-<sha8>: same filename + different content -> different job/state."""
    stem = re.sub(r"[^A-Za-z0-9_-]", "_", os.path.splitext(upload_name)[0])[:12]
    return f"img-{stem}-{hashlib.sha256(content).hexdigest()[:8]}"


_LIVE_RESULT_KEY = "live_audio_result"  # last uploaded-audio pipeline result (session only)


def _live_job_id(upload_name, content):
    """live-<stem12>-<sha8>: same filename prefix + different content -> different job/state."""
    stem = os.path.splitext(upload_name)[0][:12].replace(" ", "_")
    return f"live-{stem}-{hashlib.sha256(content).hexdigest()[:8]}"


def _image_text_extract(ocr, job_id, use_llm=False, ollama_model="llama3.2:3b"):
    """Raw OCR text -> run_text_extract(source="image").

    use_llm=True runs the LLM judge+eval (local Ollama only, gap-fill with
    verbatim proof, regex fallback); False stays regex-only. Returns (tj, ej).
    """
    return run_text_extract(ocr["text"], _ocr_segments(ocr), job_id=job_id,
                            use_llm=use_llm, ollama_model=ollama_model,
                            engine=ocr.get("engine", "easyocr"), source="image")


def _ai_eval_panel(ej):
    """Image-source AI eval: per-field PASS / MISSING / UNPROVEN table."""
    ev = (ej or {}).get("ai_eval")
    if not ev:
        return
    sm = ev.get("summary", {})
    st.markdown(f"**AI eval** (`{ev.get('engine', '')}`) — "
                f"✅ PASS {sm.get('pass', 0)} · "
                f"🟥 MISSING {sm.get('missing', 0)} · "
                f"🟨 UNPROVEN {sm.get('unproven', 0)}")
    st.caption("Gap-filled values are YELLOW — verify each against the image. "
               "UNPROVEN = no verbatim OCR line supports the value.")
    rows = [{"field": f.get("field"), "status": f.get("status"),
             "value": f.get("value"), "evidence (OCR line)": f.get("evidence_line"),
             "conf": f.get("confidence"), "note": f.get("note", "")}
            for f in ev.get("fields", [])]
    st.dataframe(rows, use_container_width=True, hide_index=True)
    with st.expander("AI eval JSON (debug)"):
        st.json(ev)


def _image_ocr_view():
    """Image source: upload -> EasyOCR -> raw text -> normalize -> regex + LLM judge/eval.

    Raw OCR text stays visible above the tabs. Normalize/extract run with
    source="image" (no speech-mishear rules, no fuzzy drug match, OCR lines
    are boundaries). With "LLM eval" on, extracted text goes through the local
    Ollama judge (ocr_llm_judge): gap-fill of missing fields with verbatim
    proof + per-field PASS/MISSING/UNPROVEN eval; any LLM failure falls back
    to regex-only. The note reuses _show_result(source="image"): corrections,
    audit, verify and downloads stay in session state; nothing is written to
    transcripts/, entities/ or exports/.
    """
    with st.sidebar:
        up = st.file_uploader("Drop .png/.jpg/.jpeg", type=[e.lstrip(".") for e in OCR_EXTS])
        ocr_ollama_model = st.selectbox("Ollama model", OLLAMA_MODELS, index=0, key="ocr_ollama_model")
        ocr_ollama_ok, ocr_ollama_reason = _ollama_available(ocr_ollama_model)
        if ocr_ollama_ok:
            st.markdown(f"🟢 **Ollama ready** — {ocr_ollama_model}")
        else:
            st.markdown(f"🔴 **Ollama unavailable** — {ocr_ollama_reason}")
        ocr_use_llm = st.checkbox("LLM eval (local Ollama, regex fallback)", value=True, key="ocr_use_llm")
        run_btn = st.button("▶ Run OCR", type="primary", use_container_width=True)
        st.caption("Image → EasyOCR → raw text → normalize → regex + LLM judge/eval → discharge note. "
                   "Nothing is saved to disk; downloads are generated in memory.")

    if up is None:
        st.info("Upload a PNG/JPG/JPEG image in the sidebar and press ▶ Run OCR.")
        return

    content = up.getvalue()
    job_id = _image_job_id(up.name, content)
    col_img, col_txt = st.columns([1, 1.2])
    with col_img:
        st.markdown("**Uploaded image**")
        try:
            st.image(content, caption=up.name)
        except Exception as e:
            st.warning(f"Preview unavailable ({type(e).__name__}); OCR will still report whether the file is readable.")

    res_key = f"ocr_{job_id}"
    if run_btn:
        ext = os.path.splitext(up.name)[1].lower()
        tmp_fd, tmp_img = tempfile.mkstemp(prefix="medibytes_ocr_", suffix=ext)
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(up.getbuffer())
            with st.spinner("Running EasyOCR (first run downloads models, ~100MB)…"):
                r = extract_text_from_image(tmp_img)
        except Exception as e:
            r = {"success": False, "text": "", "engine": "easyocr", "source_file": tmp_img,
                 "blocks": [], "lines": [], "error_code": "APP_ERROR",
                 "error": f"{type(e).__name__}: {e}"}
        finally:
            try:
                os.remove(tmp_img)
            except OSError:
                pass
        r = {**r, "source_file": up.name}  # show upload name, not the deleted temp path
        st.session_state[res_key] = r
        x = None
        if r.get("success"):
            try:
                with st.spinner("Extracting fields (regex + LLM judge)…"):
                    tj, ej = _image_text_extract(r, job_id, use_llm=ocr_use_llm,
                                                 ollama_model=ocr_ollama_model)
                # AI-original note (before any human correction), rendered in memory
                ai = fill_template(copy.deepcopy(ej), tj)
                x = (tj, ej, ai["html"] if isinstance(ai, dict) else ai)
            except Exception as e:
                x = {"error": f"{type(e).__name__}: {e}"}
        st.session_state[res_key + "_x"] = x
        _reset_note_state(job_id)  # new OCR run: editor/verify follow the new extraction

    r = st.session_state.get(res_key)
    with col_txt:
        if r is None:
            st.info("Press ▶ Run OCR to extract text from this image.")
            return
        if not r.get("success"):
            code = r.get("error_code", "UNKNOWN")
            (st.warning if code == "NO_TEXT_DETECTED" else st.error)(f"OCR failed: {code}")
            st.code(r.get("error", ""), language=None)
            if code == "ENGINE_UNAVAILABLE":
                st.caption("Install: python -m pip install easyocr==1.7.2")
            return
        lines = r.get("lines", [])
        low = min((ln["min_confidence"] for ln in lines), default=0.0)
        st.success(f"OCR ok | engine `{r.get('engine')}` | {len(lines)} lines | "
                   f"{len(r.get('blocks', []))} blocks | lowest confidence {low:.2f}")
        st.markdown("**Raw OCR text** (verbatim, uncorrected — verify against the image)")
        st.code(r.get("text", ""), language=None)

    # ---- normalize + regex/LLM extract (source="image") -> shared result tabs + note ----
    x = st.session_state.get(res_key + "_x")
    if x is None:
        return
    if isinstance(x, dict) and "error" in x:
        st.error("Text extraction failed (raw OCR above is unaffected)")
        st.code(x["error"], language=None)
        return
    tj, ej, ai_html = x
    st.caption(f"Extraction engine: {ej.get('llm_engine', '')}. Missing values stay NIL/RED — "
               "verify every value against the image. Template wording ('heard', seg 0.0-0.0s) "
               "is audio-derived and refers to OCR lines here.")
    _ai_eval_panel(ej)
    _show_result(job_id, tj, ej, None, prebuilt_html=ai_html,
                 source="image", image_bytes=content, ocr=r)


def main():
    st.set_page_config(page_title="MediBytes - voice to discharge note", layout="wide")
    st.title("MediBytes — voice → discharge note  🏥")
    st.caption("One-command investor demo (CPU-only, offline STT). Stages 0 Receive → 1 Clean → 2 Text → 3 Normalize → 4 Extract → 5 Premium A4 note. Demo only — verify before clinical use.")

    source = st.sidebar.radio("Input source", ["Audio", "Image"], horizontal=True)
    if source == "Image":
        _image_ocr_view()
        return

    with st.sidebar:
        st.header("Controls")
        sample = st.selectbox("Cached sample", ["(upload a file)"] + CACHED)
        up = st.file_uploader("Or drop .wav/.mp3/.m4a (≤100MB, ≤30min)", type=["wav", "mp3", "m4a"])
        model = st.selectbox("STT model", ["tiny-int8 (fast)", "base-int8 (better)", "small-int8 (best CPU)", "mock (instant script)"],
                             index=1)
        model_key = model.split()[0]
        template = st.selectbox("Template", ["er_discharge", "none"], index=0)
        
        # Ollama settings
        ollama_model = st.selectbox("Ollama model", OLLAMA_MODELS, index=0)
        ollama_ok, ollama_reason = _ollama_available(ollama_model)
        if ollama_ok:
            st.markdown(f"🟢 **Ollama ready** — {ollama_model}")
        else:
            st.markdown(f"🔴 **Ollama unavailable** — {ollama_reason}")
        
        use_llm = st.checkbox("LLM extraction (local Ollama, regex fallback)", value=True)
        run_btn = st.button("▶ Run pipeline", type="primary", use_container_width=True)
        st.divider()
        st.subheader("60-sec narration")
        for title, body in NARRATION:
            with st.expander(title):
                st.write(body)

    # Cached sample view (no rerun needed)
    if up is None and sample != "(upload a file)":
        job = _job_from_label(sample)
        tj, ej, meta, html = _load_cached(job)
        if not tj or not ej:
            st.warning(f"Cache missing for {job}. Upload a file and press Run, or run `python run_demo.py`.")
            return
        raw = os.path.join(_HERE, "audio_in", f"sample1_hinglish_fever.wav" if job == "demo-001"
                           else (f"sample2_cough_allergy.wav" if job == "demo-002" else ""))
        if job == "luvvoice-001" or not os.path.isfile(raw):
            raw = None
            for cand in (os.path.join(_HERE, "..", "luvvoice.com-20260921-LnWlf1.mp3"),
                         os.path.join(_HERE, "..", "demo scripts", "luvvoice.com-20260921-LnWlf1.mp3"),
                         os.path.join(_HERE, "audio_in", job + ".wav")):
                if os.path.isfile(cand):
                    raw = cand
                    break
        clean = os.path.join(_HERE, "cleaned", f"{job}.wav")
        _show_result(job, tj, ej, meta, raw_path=raw, clean_path=clean if os.path.isfile(clean) else None,
                     prebuilt_html=html)
        return

    # Live upload view
    if up is not None:
        suf = os.path.splitext(up.name)[1] or ".wav"
        tmp_in = os.path.join(tempfile.gettempdir(), f"medibytes_up{suf}")
        with open(tmp_in, "wb") as f:
            f.write(up.getbuffer())
        st.audio(tmp_in)
        job_id = _live_job_id(up.name, up.getvalue())
        if run_btn:
            with st.spinner("Stage 1 cleaning…"):
                cleaned = os.path.join(tempfile.gettempdir(), f"{job_id}_clean.wav")
                try:
                    meta = clean_audio(tmp_in, cleaned)
                except ValueError as e:
                    st.error(str(e))
                    return
            try:
                with st.spinner(f"Stages 2-4 transcribing ({model_key}) + extracting…"):
                    tj, ej = run_stt_extract(cleaned, job_id, use_llm=use_llm, model=model_key,
                                             ollama_model=ollama_model)
            except TranscriptionError as e:
                # no substitute text: nothing extracted, shown or persisted for this run
                if (st.session_state.get(_LIVE_RESULT_KEY) or {}).get("job_id") == job_id:
                    st.session_state.pop(_LIVE_RESULT_KEY, None)
                st.error(f"Speech-to-text failed — no transcript, entities or discharge note "
                         f"were produced for this file.\n\n{e}")
                return
            # Buttons are True only on the click rerun; Apply corrections / Verify
            # call st.rerun(), so the result must live in session state to survive.
            # A fresh run replaces this job's earlier AI original / edits / verify.
            _reset_note_state(job_id)
            st.session_state[_LIVE_RESULT_KEY] = {"job_id": job_id, "tj": tj, "ej": ej,
                                                  "meta": meta, "clean_path": cleaned}
            # persist for download/share
            try:
                for name, obj in ((f"transcripts/{job_id}.json", tj), (f"entities/{job_id}.entities.json", ej)):
                    fp = os.path.join(_HERE, name)
                    os.makedirs(os.path.dirname(fp), exist_ok=True)
                    with open(fp, "w", encoding="utf-8") as f:
                        json.dump(obj, f, indent=2, ensure_ascii=False)
                if template != "none":
                    prev = fill_template(ej, tj, template)
                    efp = os.path.join(_HERE, "exports", f"{job_id}.html")
                    os.makedirs(os.path.dirname(efp), exist_ok=True)
                    with open(efp, "w", encoding="utf-8") as f:
                        f.write(prev["html"])
            except Exception as e:
                st.warning(f"persist skipped: {e}")
        live = st.session_state.get(_LIVE_RESULT_KEY)
        if live and live["job_id"] == job_id:  # only for this exact upload (name + content)
            _show_result(job_id, live["tj"], live["ej"], live["meta"],
                         raw_path=tmp_in, clean_path=live["clean_path"])
        else:
            st.info("Press ▶ Run pipeline to process this file.")
    else:
        st.info("Pick a cached sample in the sidebar, or upload your own audio and press ▶ Run pipeline.")


if __name__ == "__main__":
    main()
