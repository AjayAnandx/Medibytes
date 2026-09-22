"""MediBytes product demo UI: mic/file -> clean -> text -> entities -> discharge.
Run via one command:  python run_demo.py   (installs deps, warms cache, launches this UI)
Direct:  streamlit run demo/app.py
"""
import copy
import datetime
import json
import os
import sys
import tempfile
import subprocess
import shutil

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import streamlit as st

try:
    import matplotlib.pyplot as plt
    HAS_PLOT = True
except Exception:
    HAS_PLOT = False

from audio_clean import _load_mono_float, clean_audio
from stt_extract import run_stt_extract
from fill_template import fill_template

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
            c = item.get("color", "YELLOW")
            neg = " [NEGATED]" if item.get("negated") else ""
            st.markdown(
                f"<div style='border-left:6px solid {COLOR.get(c, '#999')};padding:8px;margin:6px 0'>"
                f"<b>{grp[:-1]} {c}</b>{neg} - {item.get('name') or item.get('text')} "
                f"{item.get('dose', '')} {item.get('unit', '')} {item.get('frequency', '')} "
                f"{item.get('duration', '')} ({item.get('confidence', '?')})<br>"
                f"<i>Heard:</i> {item.get('source_sentence', '')}"
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
    "cc_blank",
    "drug_0_name", "drug_0_dose", "drug_0_unit", "drug_0_freq", "drug_0_duration",
    "drug_1_name", "drug_1_dose", "drug_1_unit", "drug_1_freq", "drug_1_duration",
    "vitals_bp_sys", "vitals_bp_dia", "vitals_temp", "vitals_spo2",
    "allergy_line", "dx_text", "dx_code_box", "fu_text", "sign_line",
)
_SLOT_LABELS = {
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


def _parse_dose(raw):
    raw = (raw or "").strip()
    if raw.upper().startswith("NIL") or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return raw  # keep physician text; coords renders it verbatim


def _apply_edits(orig_ej, edits):
    """Patch a deepcopy of the AI entities from slot_key -> new text.

    Returns (patched_entities, audit_entries). Never mutates transcripts.
    """
    patched = copy.deepcopy(orig_ej)
    audit = []
    ts = datetime.datetime.now().isoformat(timespec="seconds")

    def _record(slot_key, path, old, new):
        if str(old) != str(new):
            audit.append({"ts": ts, "slot_key": slot_key, "entity_path": path,
                          "original_ai": old, "human_corrected": new})

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
        row["color"] = "YELLOW"
        row["confidence"] = max(float(row.get("confidence", 0) or 0), 0.85)

    # vitals -> structured_vitals (authoritative for coords when present)
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

    # allergies active line -> ;-separated replace (negated DENIED stay locked)
    if "allergy_line" in edits:
        new = edits["allergy_line"].strip()
        old_active = "; ".join(a.get("text", "") for a in patched.get("allergies", [])
                               if not a.get("negated"))
        _record("allergy_line", "allergies.active", old_active, new)
        patched["allergies"] = [a for a in patched.get("allergies", []) if a.get("negated")]
        for part in [p.strip() for p in new.split(";") if p.strip()
                     and not p.strip().upper().startswith("NIL")]:
            patched["allergies"].append({"text": part, "negated": False, "confidence": 0.9,
                                         "color": "YELLOW", "source_sentence": "",
                                         "note": "human-corrected"})

    # diagnosis + follow-up + signature
    dx = patched.setdefault("diagnosis", {})
    if "dx_text" in edits:
        _record("dx_text", "diagnosis.text", dx.get("text", ""), edits["dx_text"].strip())
        dx["text"] = edits["dx_text"].strip()
        dx["color"] = "YELLOW"
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


def _show_result(job_id, tj, ej, meta, raw_path=None, clean_path=None, prebuilt_html=None):
    st.success(f"Job `{job_id}`  |  lang `{tj.get('language')}`  |  engine `{tj.get('stt_engine')}`")
    steps = ["receive ok", f"clean vad={meta.get('vad_ratio')}" if meta else "clean",
             f"{len(tj.get('segments', []))} segments", f"{len(ej.get('drugs', []))} drugs",
             "discharge ready"]
    st.write("  →  ".join(f"✅ {s}" for s in steps))

    tab_audio, tab_text, tab_ent, tab_note = st.tabs(["Audio", "Transcript", "Entities", "Discharge note"])
    with tab_audio:
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
            st.json({k: meta.get(k) for k in ("duration_s", "vad_ratio", "snr_before_db", "snr_after_db", "denoise") if k in meta})
    with tab_text:
        st.code(tj.get("text", ""))
        st.success(tj.get("normalized_en", ""))
        with st.expander(f"Segments + word times ({len(tj.get('segments', []))})"):
            st.json(tj.get("segments", []))
    with tab_ent:
        _entity_cards(ej)
    with tab_note:
        # Editable discharge: left = field boxes prefilled with AI values,
        # right = re-rendered A4 preview. PDF/print is a snapshot of the
        # VERIFIED corrected note, never the primary surface.
        try:
            orig_key, edits_key, ver_key = f"{job_id}_orig", f"{job_id}_edits", f"{job_id}_verified"
            if orig_key not in st.session_state:
                st.session_state[orig_key] = copy.deepcopy(ej)
                st.session_state[edits_key] = {}
                st.session_state[ver_key] = False
            orig_ej = st.session_state[orig_key]
            saved_edits = st.session_state[edits_key]

            ai_defaults, _r0 = _slot_defaults(orig_ej, tj)
            # working entities = AI + saved human edits
            work_ej, _ = _apply_edits(orig_ej, saved_edits) if saved_edits else (orig_ej, [])

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
                    patched, new_audit = _apply_edits(orig_ej, {**saved_edits, **form_vals})
                    # keep only real diffs vs AI original
                    st.session_state[edits_key] = {k: v for k, v in {**saved_edits, **form_vals}.items()
                                                   if str(v) != str(ai_defaults.get(k, ""))}
                    if new_audit:
                        _persist_corrected(job_id, patched, new_audit)
                        st.session_state[ver_key] = False  # new edits reset verification
                        st.success(f"Saved {len(new_audit)} correction(s). Review the preview, then Verify.")
                    else:
                        st.info("No changes vs AI original.")
                    st.rerun()

                # re-resolve AFTER edits for RED count + preview
                cur_ej, _ = _apply_edits(orig_ej, saved_edits) if saved_edits else (orig_ej, [])
                _d, r_cur = _slot_defaults(cur_ej, tj)
                reds = [s for s in r_cur.get("slots", []) if s.get("color") == "RED"]
                n_audit = 0
                try:
                    afp0 = os.path.join(_HERE, "entities", f"{job_id}.audit.json")
                    if os.path.isfile(afp0):
                        with open(afp0, encoding="utf-8") as f:
                            n_audit = len(json.load(f))
                except Exception:
                    pass
                st.caption(f"Audited corrections: {n_audit} | RED missing: {len(reds)}")
                can_verify = n_audit > 0 and len(reds) == 0 and not st.session_state[ver_key]
                if st.session_state[ver_key]:
                    st.success("Verified — corrected exports unlocked.")
                elif st.button("Verify & unlock corrected exports",
                               disabled=not can_verify, use_container_width=True,
                               key=f"verify_{job_id}"):
                    st.session_state[ver_key] = True
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
                            efp = os.path.join(_HERE, "exports", f"{job_id}.corrected.html")
                            try:
                                os.makedirs(os.path.dirname(efp), exist_ok=True)
                                with open(efp, "w", encoding="utf-8") as f:
                                    f.write(html)
                                with open(os.path.join(_HERE, "exports", f"{job_id}.corrected.docx"), "wb") as f:
                                    f.write(docx_bytes)
                            except Exception as e:
                                st.warning(f"persist skipped: {e}")
                            st.download_button("Download CORRECTED DOCX (verified)", docx_bytes,
                                               file_name=f"{job_id}.corrected.docx")
                    else:
                        st.caption("Corrected exports locked until Verify (no RED + audited). Print/PDF only from the verified note.")
                except Exception as e:
                    st.warning(f"preview skipped: {e}")
            with st.expander("Show Source links"):
                for s in tj.get("segments", []):
                    st.write(f"seg {s.get('id')} [{s.get('start')}-{s.get('end')}s] : {s.get('text')}")
            with st.expander("Audit trail (AI vs human)"):
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


def main():
    st.set_page_config(page_title="MediBytes - voice to discharge note", layout="wide")
    st.title("MediBytes — voice → discharge note  🏥")
    st.caption("One-command investor demo (CPU-only, offline STT). Stages 0 Receive → 1 Clean → 2 Text → 3 Normalize → 4 Extract → 5 Premium A4 note. Demo only — verify before clinical use.")

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
        if run_btn:
            job_id = "live-" + os.path.splitext(up.name)[0][:12].replace(" ", "_")
            with st.spinner("Stage 1 cleaning…"):
                cleaned = os.path.join(tempfile.gettempdir(), f"{job_id}_clean.wav")
                try:
                    meta = clean_audio(tmp_in, cleaned)
                except ValueError as e:
                    st.error(str(e))
                    return
            with st.spinner(f"Stages 2-4 transcribing ({model_key}) + extracting…"):
                tj, ej = run_stt_extract(cleaned, job_id, use_llm=use_llm, model=model_key, ollama_model=ollama_model)
            _show_result(job_id, tj, ej, meta, raw_path=tmp_in, clean_path=cleaned)
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
        else:
            st.info("Press ▶ Run pipeline to process this file.")
    else:
        st.info("Pick a cached sample in the sidebar, or upload your own audio and press ▶ Run pipeline.")


if __name__ == "__main__":
    main()
