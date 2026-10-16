# MediBytes — Daily Work Report, 16 October 2026

Branch: `fix/bug-cleanup` · Scope: remaining-bug and quality cycle, ER discharge note first.
MediBytes is a demonstration prototype. Nothing in this report means it is clinically validated or production-ready.
All testing used synthetic text, synthetic audio/images and temporary databases.

---

## 1. Objectives

1. Audit the branch state, establish a test baseline and build a prioritised bug list.
2. Make the ER discharge note (HTML and DOCX) complete, readable on A4, and honest about provenance and verification.
3. Test audio, OCR and IPD paths with synthetic inputs and fix what breaks.
4. Add a regression test for every confirmed bug, run the full suite, commit and push to `origin/fix/bug-cleanup` only.

## 2. Baseline (start of day)

| Item | Value |
|---|---|
| Branch | `fix/bug-cleanup` (HEAD `c528531` = `origin/fix/bug-cleanup`) |
| `main` / `origin/main` | `3e3fbab` |
| Working tree | clean, except 3 untracked live-upload outputs (`demo/{transcripts,entities,exports}/live-rec-0681e7e8.*`, intentionally left untracked) |
| Test suite | `python -m pytest demo/tests -q`: **442 passed, 40 subtests passed** |

## 3. Bugs discovered

Severity: Critical = clinical content silently lost or changed; High = clinically important content hidden or wrong; Medium = misleading or incomplete; Low = cosmetic or usability.

| ID | Sev. | Bug | How to reproduce (synthetic) |
|---|---|---|---|
| C1 | Critical | The discharge note printed only 2 medicine rows. Medicines 3 and later were silently dropped from HTML and DOCX. | Dictate 3+ medicines with doses and render the note. |
| C2 | Critical | Fuzzy drug matching replaced an unknown drug with a different known drug ("phenyzo peridine 200 mg" became **cetirizine 200 mg**, match ratio 0.71). | Text mentioning "peridine 200 mg". |
| H1 | High | An unknown medicine with a dose ("nitrofuurentoin 100 mg") was silently dropped. | Real faster-whisper transcript of a synthetic UTI dictation. |
| H2 | High | Values were cut with "…" (diagnosis, follow-up, allergy list, "101 degrees Fahren"). | Long diagnosis or follow-up text. |
| H3 | High | Safety notes (AI-suggested, CONFLICT, multiple readings) were clipped by fixed-height boxes. | BP 150/95 then 130/80, plus an AI-filled field. |
| H4 | High | Vital-sign values were printed without labels, and static template text overlapped values. | Any note printed to A4. |
| H5 | High | "two / three / four times a day" frequencies were lost (number words are converted to digits before frequency matching). | "paracetamol 500 mg three times a day". |
| H6 | High | An OCR medication line with a misread dose ("1DD mg") disappeared without trace. | Real EasyOCR on a synthetic handwriting-style image. |
| M1 | Medium | The note had no draft/verified status, verification time, correction count or fingerprint. | Render before and after Verify. |
| M2 | Medium | Source text was not HTML-escaped (`<`, `&`). | "BP <90 & falling". |
| M3 | Medium | Missing follow-up was shown GREEN ("no follow-up heard"). | Note without follow-up. |
| M4 | Medium | "once a day" was not recognised as a frequency. | "cetirizine 10 mg once a day". |
| M5 | Medium | "99.2 Fahrenheit" and "Temp: 99.2 F" were not captured as temperature. | Those phrases. |
| M6 | Medium | Heart rate / pulse was never shown on the note or as an IPD event. | "heart rate of 88". |
| M7 | Medium | A filler word was taken as the drug name ("in thromise" became drug "in"). | "give in thromise 500 mg". |
| M8 | Medium | The correction editor and AI judge only covered medicines 1–2. | 3+ medicines, then open the editor. |
| M9 | Medium | The "AI-original HTML" download could be stale (pre-built HTML from an earlier state). | Edit, verify, download the original. |
| M10 | Medium | The DOCX export was a developer key list (`[drug_0_freq] …`), not a document. | Download DOCX. |
| M11 | Medium | The header showed a fictitious hospital name ("CITY CARE HOSPITAL"). | Any note. |
| M12 | Medium | Only the first symptom was shown; denied symptoms were capped at 3. | "Cough, fever and headache. Denies chest pain and breathlessness." |
| L1 | Low | IPD "Progress note draft created" confirmation vanished after the automatic rerun. | IPD tab D → Generate. |
| L2 | Low | The diagnosis could start with "is" ("Diagnosis is …"). | "The diagnosis is acute bronchitis." |
| L3 | Low | HPI text was cut at 300 characters with no marker. | Long history. |
| L4 | Low | README and footer said the note was "single A4". | — |

Proof the regression tests catch these: run against the committed code before today's changes, `test_discharge_report.py` failed 16 of 17 tests (the 18th, app-level, was written after the fix), `test_extraction_realworld.py` failed 14 of 18 (the other 4 are guards), and `test_ipd_messages.py` failed 2 of 4 (the 2 refusal-message guards pass on both versions).

## 4. Bugs fixed (files)

| Area | Files | Bugs |
|---|---|---|
| Note template | `demo/templates/er_discharge.premium.html.j2` (rewritten), `demo/templates/er_discharge.json` (version `0.3.0-document`) | C1, H2–H4, M1–M3, M11, M12, L3, L4 |
| Rendering and DOCX | `demo/fill_template.py` | C1, M1, M2, M10, L3 |
| Slot resolution | `demo/coords.py` | C1, H2, M3, M5, M6, M12 |
| Extraction | `demo/stt_extract.py` | C2, H1, H5, H6, M4, M5, M7, L2 |
| ER app | `demo/app.py` | M8 (editor), M9, verified status passed to preview and exports |
| AI judge | `demo/ocr_llm_judge.py` | M8 (judge) |
| IPD UI | `demo/ipd/ui.py` | L1 |
| Docs | `README.md` | L4, printing guidance |

Safety rules followed throughout: no medicine is dropped; nothing missing is filled with a default (missing stays NIL / NOT DOCUMENTED and RED or YELLOW); AI suggestions stay labelled "AI-suggested - verify" and are never marked verified; verification invalidation, audit trail and fingerprinting are unchanged.

## 5. Discharge note: before and after

| Aspect | Before | After |
|---|---|---|
| Layout | Fixed single-A4 grid; boxes clipped content | Flowing A4 document, 14/13/15 mm margins, continues onto further pages |
| Medicines | 2 rows maximum | Every medicine; table header repeats on each page (checked with 28 medicines over 2 pages) |
| Long values | Cut with "…" | Printed in full (`fit_text` never truncates) |
| Notes | Often clipped | AI-suggested, CONFLICT, multiple-readings notes always visible |
| Vitals | Unlabelled numbers | "BP systolic (mmHg)", "BP diastolic (mmHg)", "Temperature", "SpO2", plus "Other recorded vitals" (e.g. heart rate) |
| Allergies | Status only | Status, allergens, "Documented as not allergic" list, conflicts listed for review |
| Unrecognised medicines | Silently dropped | Red box "Possible medications not recognised…", plus a review item with the source sentence |
| Status | None | "AI-ASSISTED DRAFT — awaiting clinician verification" with red/yellow/AI counts, or "CLINICIAN-VERIFIED (DEMO)" with time, number of audited corrections and note fingerprint. Draft HTML never contains the word "verified". |
| Source text | Unescaped | Escaped; over 2,500 characters is shortened with an explicit marker |
| Footer | None | "MediBytes ER discharge summary · job id" and "Page X of Y" |
| Header | Fictitious hospital | "MediBytes demonstration prototype · AI-assisted documentation draft" |
| DOCX | Developer key list | Structured A4 document with the same sections, a medication table, red/yellow highlighting and grey notes |

Visual inspection: HTML notes were printed to PDF with headless Chrome 155 and inspected as images:

- before/after stress sample
- verified banner
- 8 medicines
- minimal note
- 28 medicines (2 pages)
- a real-audio UTI note
- a real-OCR handwriting note

## 6. Audio testing

| Test | Model | Result |
|---|---|---|
| Synthetic dictation as WAV, MP3, M4A | **Real** faster-whisper `base` (local cache, CPU) | Transcribed in about 2–6 s per clip; all three formats decoded with duration validated |
| Silence | Real | Rejected, no fabricated transcript |
| Unknown drug "nitrofuurentoin" | Real | Led to fix H1: now surfaced as "not recognised" |
| Multiple BP readings, allergy plus "no known allergies" conflict, corrections, exports | Mocked transcript (fixed text) through the real extraction and Streamlit AppTest | Warnings and conflicts shown; corrected exports carry the audit count |

No word-error rate or accuracy figure was measured. The results above are functional checks on a handful of synthetic clips.

## 7. OCR testing

| Test | Engine | Result |
|---|---|---|
| Synthetic printed prescription (patient info, BP, date, drugs, doses, frequencies) | **Real** EasyOCR (local models) | Read correctly; BP not confused with the date |
| Synthetic handwriting-style image (Ink Free font) | Real EasyOCR | Misreads occur. An out-of-range temperature (90.2 F) is left as NIL, not used. The "1DD mg" medication line is now surfaced for review (fix H6). |
| "Temp: 99.2 F", "three times a day", unknown medicine lines, negated mentions | Text fixtures through image-source extraction | Covered by new regression tests |
| AI evidence / judge on medicines 3+ | Fake LLM response | Judge now evaluates every medicine row |

Handwriting-style fonts are not real handwriting. OCR accuracy on real clinical documents is unmeasured.

## 8. IPD testing

An end-to-end Streamlit AppTest run of all six tabs (A–F) used a temporary SQLite database (`MEDIBYTES_IPD_DB` pointing to a temp folder):

- **A:** admission; a mismatched ward on the same MRN was refused.
- **B:** a round note was processed; the duplicate was detected.
- **C:** timeline.
- **D:** progress note generated (found L1: the confirmation vanished).
- **E:** approval.
- **F:** 2 downloads present.

Heart rate in a round note now becomes a `pulse` vital event. The real IPD database (`%LOCALAPPDATA%\MediBytes\ipd_demo.sqlite`) was not touched (hash checked before and after).

## 9. New regression tests (40)

| File | Tests | Covers |
|---|---|---|
| `demo/tests/test_discharge_report.py` | 18 | C1 (3/4/12/28 medicines, HTML and DOCX), H2, H3, H4, M1–M3, M8, M11, M12, escaping, long-source marker, every slot value in both formats, and an app-level flow: edit medicine 3 → verify → corrected HTML/DOCX export |
| `demo/tests/test_extraction_realworld.py` | 18 | C2, H1, H5, H6, M4–M7, L2, genuine misspellings still matched, negated mentions not listed, no duplicate lines |
| `demo/tests/test_ipd_messages.py` | 4 | L1, refusal messages remain visible, heart rate → pulse event |

`demo/tests/test_ai_provenance.py` was updated so its DOCX check reads the new medication table.

## 10. Final test results

`python -m pytest demo/tests -q` → **482 passed, 40 subtests passed, 0 failed** (14 matplotlib deprecation warnings). That is 442 at baseline plus 40 new tests.

## 11. Manual and visual testing

| Check | Status |
|---|---|
| HTML note printed to A4 PDF (headless Chrome) and inspected | Done (samples listed in section 5) |
| DOCX structure (A4 size, headings, all medicine rows, highlights, status text) | Done programmatically with python-docx |
| DOCX rendered visually in Word | **Not done.** Word automation hung twice (probably a hidden dialog); only the automation instances were stopped. |
| Interactive browser session of the Streamlit apps | **Not done.** App flows were covered with Streamlit AppTest instead. |
| Local LLM (Ollama) paths | **Not run today.** Tested with fixed fake responses only. |

## 12. Unresolved limitations

- Unrecognised medicines block Verify only when no recognised medicine exists (red NIL row). Otherwise they appear as a red review box and a review item but do not block.
- The drug vocabulary is about 30 names; anything else is surfaced as "not recognised", not matched.
- The "not recognised" excerpt includes surrounding words from the sentence.
- STT/OCR accuracy is unmeasured; handwriting testing used a font, not real handwriting.
- "Other recorded vitals" (e.g. heart rate) are shown but not editable in the correction form.
- With many medicines the correction form becomes long (5 fields per medicine).
- Cached demo samples in `demo/exports` were not regenerated with the new template.
- IPD has no discharge workflow and no bed-occupancy check.
- DOCX visual check and an interactive browser check are still outstanding (section 11).

## 13. Commits and remote

| Item | Value |
|---|---|
| Code, tests and docs commit | `2dc6fe5` — "Improve discharge reports, complete regression fixes and QA" |
| This report | the commit that follows `2dc6fe5` on the same branch |
| Remote branch | `origin/fix/bug-cleanup` (normal push, no force) |
| `main` | not modified (`3e3fbab`) |

## 14. Summary for the project lead

The ER discharge note was the main risk. It silently printed only two medicines, cut long values, and could swap an unrecognised drug for a different known one (peridine → cetirizine). All three are fixed:

- Every medicine is printed on a multi-page A4 note and in a matching DOCX.
- Nothing is truncated.
- Unrecognised medicines are flagged for review rather than dropped or replaced.
- The note states plainly whether it is an AI-assisted draft or clinician-verified (with time, corrections and fingerprint).

Real faster-whisper and EasyOCR runs on synthetic inputs found further extraction gaps (frequencies, Fahrenheit, heart rate, misread OCR doses), which are also fixed. 40 regression tests were added; the suite is 482 passed with 0 failures.

Still outstanding: a visual DOCX check in Word, an interactive browser check, LLM paths with a real model, and any measured accuracy. The system remains a demonstration prototype.
