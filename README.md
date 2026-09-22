# MediBytes — Voice → Discharge Note (Investor Demo)

> CPU-only laptop build. Doctor speaks (or drops an audio file) → cleaned audio →
> transcript → entities → **editable** ER discharge note with a human Verify gate.
> Demo only — **verify before clinical use**. No accuracy claims, no DB/queue.

---

## 1. How the whole demo works

```
mic/file (.wav/.mp3/.m4a, ≤100MB, ≤30min)
  │ [0] Receive   pipeline.py + audio_clean.check_audio (size/duration guard, ticket ID, no duplicates)
  ▼
16k mono wav  [1] Clean  audio_clean.clean_audio (gain normalize + RMS-VAD silence skip + optional denoise)
  │ cleaned/{job}.wav + cleaned/{job}.meta.json
  ▼
text+segments [2] STT  stt_extract.transcribe (faster-whisper tiny/base/small-int8 | mock)
  │ transcripts/{job}.json  (raw text, segments + word timestamps, normalized_en)
  ▼
normalized_en [3] Normalize  stt_extract.normalize_text (HINGLISH_MAP + narrow phonetic fixes + LID tag)
  ▼
drugs/symptoms/vitals/allergies [4] Extract  stt_extract.extract_entities (regex + drug list + fuzzy)
  │   + optional Ollama tidy/primary (llama3.2:3b default, regex fallback)
  │ entities/{job}.entities.json
  ▼
ER note  [5] Template  fill_template.fill_template (Jinja2 premium A4 HTML + python-docx)
    exports/{job}.html + exports/{job}.docx
      ▲
      │  UI: demo/app.py (Streamlit) — correct fields in side-form → Verify → corrected exports
```

**Stage → file map:** `[0]`/`[1]` = `demo/audio_clean.py`, `[2]`/`[3]`/`[4]` = `demo/stt_extract.py`
(+ `demo/llm_extract.py` for the Ollama path), `[5]` = `demo/fill_template.py` +
`demo/coords.py` + `demo/templates/`, UI = `demo/app.py`, CLI = `demo/pipeline.py`,
one-command runner = `run_demo.py`.

### Numeral-accuracy highlights

- **Vitals:** decimal-aware (`100.4°F`), `of` separator (`122 of 78`), long-form
  `blood pressure is sitting at 122 of 78`, narrow phonetic fixes (`BPA → BP`,
  `130 by AC → 130 by 80`, `Rhoomair → room air`) — each flagged for human glance.
- **Medications:** extended frequency vocabulary (`twice a day`, `every 8 hours`,
  `q8h`, `as needed/PRN/SOS/stat/BD/TID`), multi-drug splitting
  (`…along with ibuprofen…` becomes row 2 instead of being lost), `Name, for Dose`
  clauses, `a/an → 1`, hours/minutes durations, generic `allergy to X` capture.
- **LLM is gap-fill only:** regex numerals always win conflicts; LLM fills fields
  regex left `NIL/RED`, with anti-hallucination gates (proof-substring check,
  digit-presence check, no invented BP/SpO2). Any Ollama failure → regex-only.
- **Golden tests:** `demo/tests/test_numerals.py` pins all of the above (5 tests).

### Editable discharge + Verify gate (replaces PDF-first flow)

1. The **Discharge note** tab shows field boxes (left) prefilled with AI values and
   the A4 preview (right). The transcript tabs are immutable proof.
2. Edit any box → **Apply corrections** → entities are patched, slots re-resolve
   (colors / `NIL RED` states recompute), preview refreshes, and an audit entry
   (`original_ai` vs `human_corrected`) is appended to `entities/{job}.audit.json`.
3. **Verify & unlock** enables only with ≥1 audited correction **and** zero RED
   fields (the 403-gate). Corrected HTML/DOCX downloads + print unlock only then.
4. Files: `entities/{job}.corrected.entities.json`,
   `exports/{job}.corrected.html` / `.docx`. AI-original HTML stays downloadable.

### Printing to PDF (snapshot only, after Verify)

Open the **corrected HTML file** directly in Chrome → `Ctrl+P` → paper **A4**,
margins **None**, **Background graphics ON** → Save as PDF. Do not print the
Streamlit iframe (it scales the A4 page).

---

## 2. Project structure

```
medibytes project/
  README.md                      # this file
  run_demo.py                    # ONE-COMMAND entry point (install + warm cache + UI)
  DEMO_CODE_AND_MODELS.md        # deep-dive: code, models, schemas, narration
  demo scripts/                  # real test audios (.mp3 examples)
  docs/                          # architecture reports (production plan, HMS, edge cases)
  demo/
    app.py                       # Streamlit product UI
    pipeline.py                  # CLI orchestrator Stages 0→5
    audio_clean.py               # Stages 0+1
    stt_extract.py               # Stages 2+3+4 (regex core)
    llm_extract.py               # Ollama primary/tidy (gap-fill only)
    fill_template.py             # Stage 5 (classic + premium + editable DOCX)
    coords.py                    # entity words → absolute mm slots (single A4)
    requirements-demo.txt        # pinned deps (Python 3.13 Windows-safe)
    templates/
      er_discharge.json          # form spec (id, version, fields)
      er_discharge.coords.json   # slot x/y/w/h per field (A4 210×297mm)
      er_discharge.premium.html.j2  # premium A4 layout (slots overlay)
      er_discharge.html.j2       # classic fallback layout
      er_discharge.schema.json   # entities validation schema
    assets/
      drug_list_mini.json        # 30-drug mini-RxNorm + aliases + phonetic seeds
    tests/
      test_numerals.py           # golden numeral regression tests
    audio_in/                    # drop inputs here (sample1/2 tones + your files)
    cleaned/                     # 16k mono wav + .meta.json per job
    transcripts/                 # transcript_json per job
    entities/                    # entities_json (+ .corrected + .audit) per job
    exports/                     # filled note .html + .docx per job
    _state/idempotency.json      # {key: job_id} double-click guard
```

---

## 3. Setup

### Prerequisites

| Need | Version / notes |
|------|-----------------|
| Python | **3.13.x** (Windows-safe wheels pinned; tested on 3.13.2) |
| pip | ships with Python |
| Ollama | optional but recommended — [ollama.com/download](https://ollama.com/download) |
| Ollama model | `llama3.2:3b` (default, accurate) or `qwen2.5:0.5b` (fast) — see below |
| Mic/audio | optional — cached samples + mock STT run with zero downloads |

### Install

```powershell
# 1) Python deps (from the project root)
python -m pip install -r demo/requirements-demo.txt

# 2) Test deps (for the golden numeral tests)
python -m pip install pytest

# 3) Ollama model (optional — pipeline falls back to regex-only without it)
ollama pull llama3.2:3b
# fast alternative:
ollama pull qwen2.5:0.5b
```

> `scipy` / `noisereduce` / `spacy` are **optional** (no Python 3.13 Windows wheels
> for some) — the code auto-skips them. See comments in `requirements-demo.txt`.

---

## 4. Run commands

### One command — full product (install + warm cache + browser UI)

```powershell
python run_demo.py
python run_demo.py --check              # setup + verify only, no browser
python run_demo.py --no-install         # deps already installed
python run_demo.py --no-llm-warm        # skip LLM during warm cache (faster startup)
```

`run_demo.py` ensures folders exist → installs deps → syntax-checks sources →
warms `demo-001`/`demo-002` cache with `--model mock --use-llm` → launches
`streamlit run demo/app.py`.

### Direct UI (after first run)

```powershell
python -m streamlit run demo/app.py --browser.gatherUsageStats false
```

Sidebar: pick a cached sample (`demo-001` fever, `demo-002` cough, `luvvoice-001`
real MP3) or upload your own file → choose STT model + Ollama model → ▶ Run.
Main tabs: **Audio** (raw vs cleaned + waveforms) · **Transcript** (raw +
normalized + segments) · **Entities** (color proof cards + negations) ·
**Discharge note** (editable field boxes + A4 preview + Verify gate + downloads).

### CLI pipeline (Stages 0→5, no UI)

```powershell
# instant mock story (zero downloads)
python demo/pipeline.py --in demo/audio_in/sample1_hinglish_fever.wav --key demo-001 --model mock --template er_discharge --use-llm
python demo/pipeline.py --in demo/audio_in/sample2_cough_allergy.wav  --key demo-002 --model mock --template er_discharge --use-llm

# real STT on this laptop (offline, CPU int8)
python demo/pipeline.py --in "luvvoice.com-20260921-LnWlf1.mp3" --key luvvoice-001 --model base-int8 --template er_discharge --use-llm

# any new file
python demo/pipeline.py --in "YOUR_FILE.mp3" --key my-003 --model base-int8 --template er_discharge --use-llm
python demo/pipeline.py --in "YOUR_FILE.mp3" --key my-003 --model tiny-int8 --template none --no-denoise

# flags
#   --model tiny-int8 | base-int8 (investor pick) | small-int8 | mock
#   --use-llm / --no-llm   (Ollama primary; mock jobs also honor --use-llm)
#   --template er_discharge | none
#   --no-denoise
#   --gen-samples           (2 synthetic tone wavs for offline plumbing tests)
```

### Golden numeral tests

```powershell
python -m pytest demo/tests/test_numerals.py -q
```

Pins: `101 degree` temp with no invented BP · `BP 130 by 80` split ·
`BPA 130 by AC` narrow phonetic + `asitromaisin→azithromycin` ·
live two-drug + `122 of 78` + `100.4°F` + `99%` · generic allergy candidate.

### Outputs per job

| File | Contents |
|------|----------|
| `demo/cleaned/{job}.wav` + `.meta.json` | 16k mono audio + `duration_s, vad_ratio, snr, denoise` |
| `demo/transcripts/{job}.json` | `text, language, segments[] (word times), normalized_en, stt_engine` |
| `demo/entities/{job}.entities.json` | `drugs[], symptoms[], vitals[], allergies[], negations[], diagnosis, followup, llm_engine` |
| `demo/entities/{job}.corrected.entities.json` | human-corrected entities (after Apply) |
| `demo/entities/{job}.audit.json` | per-slot `original_ai → human_corrected` trail |
| `demo/exports/{job}.html` / `.docx` | AI-original note |
| `demo/exports/{job}.corrected.html` / `.docx` | verified corrected note |

---

## 5. Limits

- Hard (`audio_clean.py`): **≤100MB, ≤30min** else `413`; `<0.6s`/silence/empty → `422 NO_AUDIO`.
- Practical (CPU-only laptop): 20–30s clips → 5–25s; `llama3.2:3b` extraction adds ~30–60s per job (use `qwen2.5:0.5b` or `--no-llm` for speed).
- STT mishears are repaired narrowly (`BPA→BP`, `AC→80` in BP context only) and flagged YELLOW — physician must glance.
- LLM never overwrites regex numerals; it gap-fills fields regex left missing.

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `422 DECODE_FAIL` on mp3 | `pip install imageio-ffmpeg soundfile` (bundled exe, no admin) |
| `ModuleNotFoundError: faster_whisper` | install it, or use `--model mock` |
| `ResolutionImpossible` for spacy/scipy/noisereduce | they are OPTIONAL — code auto-skips; just re-run the requirements install |
| Ollama tidy/primary empty or slow | 3b model needs ~30–60s; check `ollama list` shows the model; uncheck LLM box for regex-only |
| `LLM JSON unparsable` in logs | transient streaming artifact — primary retries 3× then falls back to regex; harmless |
| Streamlit missing / port busy | `python run_demo.py` installs it; rerun with a free port via `--server.port 8502` |
| `422 digital silence` | mic-muted/zeros file — re-record with close mic |

## 7. Investor narration (30s × 6 ≈ 3min)

0. *"Doctor taps record or drops a file — size/duration guard, ticket ID, no duplicates."*
1. *"Wards are noisy — 16k mono, silence skip, hiss cut. Listen before/after."*
2. *"Offline speech-to-text on this laptop — every word timestamped."*
3. *"Hinglish fixed — bukhar→fever, BID→twice daily. Hospitals get clean English."*
4. *"Medicines with proof — confidence + heard sentence. ‘No penicillin’ correctly NEGATED."*
5. *"Correct the fields beside the note, Verify, then download — HTML now, DOCX, print→PDF. Demo only."*

Closer: *"Voice to verified discharge note in seconds, on one laptop, fully private."*

## 8. Skipped from production (by design)

FerroTERM + RxNorm-47K/SNOMED/ICD-10 checks, dose/UNIT_FLIP safety rules, 99%
Field-Accuracy gate + gold-600 eval, human 403 gate + dual review, DOCX→PDF/A via
Gotenberg, WORM audit, Postgres/MinIO/Redis/BullMQ, feature flags, shadow/canary,
OTEL/Prometheus/Grafana. Files (`cleaned/`, `transcripts/`, `entities/`,
`exports/`, `_state/`) are the demo DB. See `docs/` for the production plan.
