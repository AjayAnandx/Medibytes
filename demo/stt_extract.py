"""Stages 2/3/4 - STT + Normalize + Extract. CPU-only demo with graceful fallbacks.

Report: Sec 4.4 (STT faster-whisper), 4.5 (normalize), 5 (NER ensemble).
Demo: faster-whisper tiny/base-int8 if installed else deterministic mock;
normalize via unicode-range LID + hardcoded Hinglish map; extract via regex
+ drug_list_mini.json + optional spaCy-sm + optional Ollama qwen2.5:0.5b tidy.
"""
import json
import math
import os
import re
import subprocess

_HERE = os.path.dirname(os.path.abspath(__file__))

# ---- Mock transcripts so investor demo runs with zero model downloads ----
MOCK_TEXTS = {
    "demo-001": "Patient ko fever hai, bukhar 101 degree, give paracetamol 500 mg BID, do time, 3 din tak. No allergy.",
    "demo-002": "Khansi hai, cough for 5 days, BP 130 by 80, give azithromycin 500 mg once daily 3 days. No penicillin allergy, patient denies chest pain.",
    "sample1": "Patient ko fever hai, bukhar 101 degree, give paracetamol 500 mg BID, do time, 3 din tak. No allergy.",
    "sample2": "Khansi hai, cough for 5 days, BP 130 by 80, give azithromycin 500 mg once daily 3 days. No penicillin allergy, patient denies chest pain.",
}

HINGLISH_MAP = [
    ("bukhar", "fever"), ("बुखार", "fever"), ("khansi", "cough"), ("खांसी", "cough"),
    ("do time", "twice daily"), ("din tak", "days"), ("din", "days"),
    ("BID", "twice daily"), ("OD", "once daily"), ("TDS", "thrice daily"),
    ("500 मिलीग्राम", "500 mg"), ("मिलीग्राम", "mg"), ("माइक्रोग्राम", "mcg"),
]

SYMPTOMS = ["fever", "cough", "chest pain", "headache", "vomiting", "diarrhea",
            "breathlessness", "sore throat", "cold", "pain", "bukhar", "khansi"]
# Numeral-tuned vitals: decimals, 'of' separator, long-form 'blood pressure',
# temp with F/C units, SpO2 with room-air mishear tolerance.
TEMP_NUM = r"\d{2,3}(?:\.\d{1,2})?\s*(?:degrees?(?:\s*(?:fahrenheit|celsius|F|C))?|°\s*[FC]?|fahrenheit|celsius)"
VITALS_PAT = re.compile(
    r"((?:BP|B[\s-]*P|blood pressure)\s*(?:is\s+(?:sitting\s+at|around|about|of)?\s*)?\d{2,3}(?:\.\d{1,2})?\s*(by|/|over|of)\s*\d{2,3}(?:\.\d{1,2})?"
    r"|\b" + TEMP_NUM + r"\b"
    r"|SpO2\s*\d{2,3}(?:\.\d{1,2})?%?"
    r"|\b\d{2,3}(?:\.\d{1,2})?\s*(?:%|percent)\s*(?:on\s+(?:room\s*air|rhoomair))?"
    r"|oxygen saturation[^\d.]{0,10}\d{2,3}(?:\.\d{1,2})?\s*%?"
    r"|(?:pulse(?:\s*rate)?|heart\s*rate|HR)\s*(?:of|is|at)?\s*\d{2,3}"
    r"|temp[^\d.]{0,10}" + TEMP_NUM + r")",
    re.I,
)
NEG_PAT = re.compile(r"\b(no|not|denies|denied|denying|without|never|neither|nor|negative for|no known|don't|doesn't|aren't|isn't|wasn't|weren't|can't|cannot)\b", re.I)
DENIED_SYMPTOMS = ("chest pain", "breathlessness")
FREQ_PAT = (r"BID|OD|TDS|QID|once daily|twice daily|thrice daily|daily|B\.I\.D\.?"
            r"|once a day|twice a day|two times a day|thrice a day|three times a day"
            r"|four times a day|four times daily|at night|at bedtime"
            # extraction runs on digitised text: "three times a day" arrives as "3 times a day"
            r"|\d+\s*times?\s+(?:a|per)\s+day|\d+\s*times\s+daily"
            r"|every\s+\d+\s*(?:hours?|hrs?|minutes?|mins?)|q\d+h"
            r"|as needed|as\s+required|PRN|S\.?O\.?S\.?|stat|BD|TID")
DRUG_CTX = re.compile(
    r"(?P<name>[A-Za-z]+),?\s+(?:for\s+)?(?P<dose>\d+(?:\.\d+)?)\s*(?P<unit>mg|mcg|g|ml|U|units?|milligrams?|micrograms?|grams?|milliliters?)\b"
    r"(?:\s*(?P<freq>" + FREQ_PAT + r"))?"
    r"(?:[^.]{0,60}?(?P<dur>\d+)\s*(?P<durunit>days?|din\b|weeks?|months?|hours?|hrs?|minutes?|mins?))?",
    re.I,
)
# Words that can sit between a drug name and its dose ("thromise in 500 mg", "Tab. X 500 mg")
DOSE_FILLER = {"in", "of", "the", "a", "an", "and", "tab", "tabs", "tablet", "tablets", "cap", "caps",
               "capsule", "capsules", "syrup", "inj", "injection", "dose", "give", "start", "starting", "on"}
FUZZY_MIN = 0.8  # sound-alike drug match threshold (genuine misspellings scored >= 0.82)
# Split multi-drug sentences: "amox ... along with ibuprofen ..." -> two clauses
DRUG_SPLIT_PAT = re.compile(r"\s+(?:along with|alongwith|plus|and also|\+)\s+", re.I)
# source="image": an OCR line holding ONLY a frequency (optionally + duration),
# e.g. "Paracetamol 500 mg" / "Twice daily", belongs to the drug line above it.
FREQ_LINE_PAT = re.compile(
    r"^(?:take\s+)?(?P<freq>" + FREQ_PAT + r")"
    r"(?:\s+(?:for\s+)?(?P<dur>\d+)\s*(?P<durunit>days?|weeks?|months?))?\s*\.?$", re.I)
# Generic allergy mention: "allergy to <word>" (candidate, YELLOW; negated flag from NEG_PAT)
ALLERGY_CTX = re.compile(r"allerg(?:y|ies)\s+(?:to|of|from)\s+(?:a\s+|an\s+|the\s+)?(?P<drug>[A-Za-z]+)", re.I)
# Allergy negation is judged per clause, so "allergic to X, denies Y allergy"
# keeps X active. A sentence only splits before a negation cue / at ";" / "but".
ALLERGY_CLAUSE_SPLIT = re.compile(
    r",\s*(?=(?:no|not|denies|denied|denying|without|never|negative)\b)|;\s*|\s+but\s+", re.I)
# Documented (non-negated) allergy lists: "allergic to X and Y", "allergy to X",
# "Allergies: X, Y" (OCR label). Items are verbatim words from the source.
ALLERGY_LIST_PAT = re.compile(
    r"\b(?:allergic\s+to|allerg(?:y|ies)\s+(?:to|of|from)|allerg(?:y|ies)\s*[:\-])\s*"
    r"(?P<list>[A-Za-z][A-Za-z ,/&\-]*)", re.I)
ALLERGY_ITEM_SPLIT = re.compile(r"\s*(?:,|/|&|\band\b|\bor\b)\s*", re.I)
ALLERGY_ITEM_CUT = re.compile(r"\s+(?:which|that|since|causing|causes|caused|with|for|in|on|as|leading|gives)\b.*", re.I)
ALLERGY_NONE_WORDS = {"none", "nil", "nka", "nkda", "none known", "no known", "nothing", "no"}
# Explicitly documented "no known allergies" (never inferred from silence).
# A drug-specific denial ("no allergy to penicillin") is NOT NKA.
NKA_PAT = re.compile(
    r"\b(?:no|nil|denies|denied|without)\s+(?:any\s+)?(?:known\s+)?(?:drug\s+|medication\s+)?"
    r"allerg(?:y|ies)\b(?!\s+(?:to|of|from|with|for)\b)"
    r"|\ballerg(?:y|ies)\s*[:\-]\s*(?:none|nil|no\s+known|nkda|nka)\b"
    r"|(?-i:\bNKD?A\b)", re.I)
NKA_MARKER = {"span": "No allergy", "negated": True, "note": "rule stand-in for CAN-BERT"}
UCUM = {"milligram": "mg", "milligrams": "mg", "microgram": "mcg", "micrograms": "mcg",
        "gram": "g", "grams": "g", "milliliter": "ml", "milliliters": "ml",
        "units": "U", "unit": "U"}
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
# source="image": each OCR line is also a boundary (audio text never has "\n")
OCR_LINE_SPLIT = re.compile(r"(?<=[.!?])\s+|\s*\n\s*")
# source="image" only, matched per OCR line. Label + value must share one line.
IMG_PATIENT_PAT = re.compile(
    r"^\s*(?:patient(?:'s)?(?:\s+name)?|name)\s*[:\-]\s*(?P<name>[A-Za-z][A-Za-z .'\-]{1,59})\s*$", re.I)
IMG_PATIENT_STOP = {"name", "date", "age", "sex", "gender", "address", "phone", "mobile", "dob",
                    "id", "no", "mrn", "uhid", "ward", "bed", "doctor", "dr", "signature"}
IMG_TEMP_PAT = re.compile(
    r"\btemp(?:erature)?\b\s*[:\-]?\s*(?P<val>\d{2,3}(?:\.\d{1,2})?)\s*°?\s*(?P<unit>[FC])\b", re.I)
IMG_TEMP_RANGE = {"F": (93.0, 110.0), "C": (34.0, 43.5)}  # outside -> not extracted (never corrected)

_ONES = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
         "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
         "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
         "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
         "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_NUMW = sorted(list(_ONES) + list(_TENS) + ["hundred", "and"], key=len, reverse=True)
_NUMW_RUN = re.compile(r"\b(?:%s)(?:[ -](?:%s))*\b" % ("|".join(_NUMW), "|".join(_NUMW)), re.I)


def _parse_numwords(toks):
    toks = [t for t in toks if t != "and"]
    if len(toks) >= 2 and toks[0] in _ONES and 1 <= _ONES[toks[0]] <= 9 and toks[1] in _TENS:
        total, toks = _ONES[toks[0]] * 100, toks[1:]  # "one thirty" -> 130
    else:
        total = 0
    cur = 0
    for w in toks:
        if w == "hundred":
            cur = (cur or 1) * 100
        elif w in _TENS:
            cur += _TENS[w]
        elif w in _ONES:
            cur += _ONES[w]
    return total + cur


def numwords_to_digits(text):
    """Dictation words -> digits: five-hundred milligram -> 500 milligram."""
    def rep(m):
        toks = re.split(r"[ -]", m.group(0).lower())
        toks = [t for t in toks if t]
        if not any(t in _ONES or t in _TENS or t == "hundred" for t in toks):
            return m.group(0)
        v = _parse_numwords(toks)
        return str(v) if v > 0 else m.group(0)
    return _NUMW_RUN.sub(rep, text)


def _load_drug_list():
    for p in (os.path.join(_HERE, "assets", "drug_list_mini.json"),
              os.path.join(_HERE, "..", "demo", "assets", "drug_list_mini.json")):
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    return [{"name": "paracetamol"}, {"name": "azithromycin"}]


def _mock_segments(text, lang="mix"):
    """Fake word timestamps evenly spaced - enough for click-word demo."""
    words, t, segs, seg_id = text.split(), 0.2, [], 0
    for sent in SENT_SPLIT.split(text):
        sw = sent.split()
        if not sw:
            continue
        dur = max(0.6, 0.32 * len(sw))
        wlist = [{"w": w, "s": round(t + i * 0.32, 2), "e": round(t + (i + 1) * 0.32, 2)}
                 for i, w in enumerate(sw)]
        segs.append({"id": seg_id, "text": sent, "start": round(t, 2),
                     "end": round(t + dur, 2), "lang": lang,
                     "confidence": 0.9, "words": wlist})
        t += dur + 0.3
        seg_id += 1
    return segs


CONFIDENCE_BASIS = "exp(avg_logprob): Whisper mean token probability, uncalibrated heuristic"


def _segment_confidence(avg_logprob):
    """Whisper segment avg_logprob (<= 0) -> exp() in [0, 1], 2 dp.

    A model-derived heuristic (geometric-mean token probability), NOT a
    calibrated probability that the text is correct. Missing, non-numeric,
    non-finite or positive (impossible log-prob) -> None; never invented.
    """
    if isinstance(avg_logprob, bool) or not isinstance(avg_logprob, (int, float)):
        return None
    if not math.isfinite(avg_logprob) or avg_logprob > 0:
        return None
    return round(math.exp(avg_logprob), 2)


class TranscriptionError(RuntimeError):
    """Real speech-to-text failed. Callers must show it, never substitute text."""


def transcribe(clean_wav, job_id="demo-001", model="tiny-int8"):
    """Stage 2. Real faster-whisper; mock scripts ONLY when model="mock" is requested.

    Mock output is a synthetic demo script (engine starts with "mock"; the UI
    and CLI label it as synthetic). Any Whisper failure (not installed, model load, decoding) raises
    TranscriptionError — a synthetic script is never returned for real audio.
    """
    if model == "mock":
        key = job_id if job_id in MOCK_TEXTS else "demo-001"
        return {"text": MOCK_TEXTS[key], "segments": _mock_segments(MOCK_TEXTS[key]),
                "engine": "mock (forced --model mock)", "language": "mix"}
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise TranscriptionError(
            f"transcription failed: faster-whisper is not installed ({e}). "
            "Install it (pip install -r demo/requirements-demo.txt) or explicitly "
            "choose the synthetic mock model.") from e
    try:
        size = {"tiny-int8": "tiny", "base-int8": "base", "small-int8": "small"}.get(model, "tiny")
        wm = WhisperModel(size, device="cpu", compute_type="int8")
        segments, info = wm.transcribe(
            clean_wav, beam_size=1, word_timestamps=True,
            no_speech_threshold=0.6, compression_ratio_threshold=2.4,
        )
        segs, full = [], []
        for i, s in enumerate(segments):
            full.append(s.text.strip())
            segs.append({"id": i, "text": s.text.strip(), "start": round(s.start, 2),
                         "end": round(s.end, 2), "lang": getattr(info, "language", "en"),
                         "confidence": _segment_confidence(getattr(s, "avg_logprob", None)),
                         "confidence_basis": CONFIDENCE_BASIS,
                         "words": [{"w": w.word, "s": round(w.start, 2), "e": round(w.end, 2)}
                                   for w in (s.words or [])]})
            # hallucination guard: thank-you loops on silence -> cap low, never raise
            c = segs[-1]["confidence"]
            if re.search(r"(thank you\s*){3,}", s.text, re.I) and c is not None:
                segs[-1]["confidence"] = min(c, 0.2)
        text = " ".join(full)
        return {"text": text, "segments": segs, "engine": f"faster-whisper:{size}-cpu-int8",
                "language": getattr(info, "language", "en")}
    except Exception as e:
        raise TranscriptionError(
            f"transcription failed ({model}): {type(e).__name__}: {e}") from e


def detect_lang_tag(text):
    hi = len(re.findall(r"[\u0900-\u097F]", text))
    ta = len(re.findall(r"[\u0B80-\u0BFF]", text))
    hinglish = bool(re.search(r"\b(ko|hai|din|tak|do time|bukhar|khansi)\b", text, re.I))
    scripts = sum([hi > 0, ta > 0, bool(re.search(r"[A-Za-z]", text))])
    if scripts > 1 or (hinglish and re.search(r"[A-Za-z]", text)):
        return "mix"
    if ta:
        return "ta-IN"
    if hi:
        return "hi-IN"
    return "en-IN"


def normalize_text(text, source="audio"):
    """Stage 3: Hinglish map + UCUM-ish unit spacing + mix tag.

    source="image": OCR lines are normalized one by one (no rule spans a line
    break) and the speech-mishear rules (BPA, by AC, Rhoomair) are skipped so
    nothing absent from the page is invented. Audio path is unchanged.
    """
    if source == "image" and "\n" in text:
        per = [normalize_text(ln, source="image") for ln in text.split("\n")]
        return {"normalized_en": "\n".join(p["normalized_en"] for p in per),
                "normalizations": [n for p in per for n in p["normalizations"]],
                "lang_tag": detect_lang_tag(text)}
    speech = source != "image"
    normalizations = []
    out = text
    for src, dst in HINGLISH_MAP:
        # word-boundary replace stops OD matching inside blood/method
        pat = r"\b" + re.escape(src) + r"\b"
        if re.search(pat, out, flags=re.I):
            out = re.sub(pat, dst, out, flags=re.I)
            normalizations.append({"from": src, "to": dst})
    out = re.sub(r"(\d+)\s*(mg|mcg|ml|g)\b", r"\1 \2", out)
    out = re.sub(r"\bB[\s-]*P\b", "BP", out)  # B-P -> BP
    # Narrow phonetic: BPA + digits -> BP (ward STT mishear, e.g. "BPA 130 by AC")
    if speech and re.search(r"\bBPA\s*\d", out, flags=re.I):
        out = re.sub(r"\bBPA(?=\s*\d)", "BP", out, flags=re.I)
        normalizations.append({"from": "BPA", "to": "BP"})
    # Narrow phonetic: "<sys> by AC" -> "<sys> by 80" (AC misheard for eighty)
    if speech and re.search(r"\d\s*(?:by|/|over|of)\s*AC\b", out, flags=re.I):
        out = re.sub(r"(\d\s*(?:by|/|over|of)\s*)AC\b", r"\g<1>80", out, flags=re.I)
        normalizations.append({"from": "by AC", "to": "by 80"})
    # Room-air mishear: Rhoomair -> room air (SpO2 sentence only)
    if speech and re.search(r"rhoomair", out, flags=re.I):
        out = re.sub(r"rhoomair", "room air", out, flags=re.I)
        normalizations.append({"from": "Rhoomair", "to": "room air"})
    before_spo2 = out
    out = re.sub(r"\bS[\s-]*P[\s-]*O[\s-]*(two|2)\b", "SpO2", out, flags=re.I)
    if out != before_spo2:
        normalizations.append({"from": "S-P-O-two", "to": "SpO2"})
    # q8h-style shorthand -> words before digitization
    if re.search(r"\bq(\d+)h\b", out, flags=re.I):
        out = re.sub(r"\bq(\d+)h\b", r"every \1 hours", out, flags=re.I)
        normalizations.append({"from": "qNh", "to": "every N hours"})
    # BD/TID shorthand (word-boundary safe)
    for _src, _dst in (("BD", "twice daily"), ("TID", "thrice daily")):
        if re.search(r"\b" + _src + r"\b", out):
            out = re.sub(r"\b" + _src + r"\b", _dst, out)
            normalizations.append({"from": _src, "to": _dst})
    out = numwords_to_digits(out)  # dictation words -> digits for extraction
    out = re.sub(r"(\d+(?:\.\d+)?)\s*(by|over)\s*(\d+(?:\.\d+)?)", r"\1/\3", out)  # BP 130 by/over 80
    # 'of' separator ONLY in BP context ( "... BP ... 122 of 78" / "blood pressure ... 122 of 78" )
    out = re.sub(
        r"((?:BP|blood pressure)[^.]{0,40}?\d+(?:\.\d+)?)\s+of\s+(\d+(?:\.\d+)?)",
        r"\1/\2", out, flags=re.I,
    )
    tag = detect_lang_tag(text)
    return {"normalized_en": out, "normalizations": normalizations, "lang_tag": tag}


def _negated(sentence):
    return bool(NEG_PAT.search(sentence))


def extract_entities(text, normalized_en, segments, source="audio"):
    """Stage 4 regex core. Returns demo entities_json.

    source="image": every OCR line is a sentence boundary (negation, drug,
    diagnosis and follow-up never span lines) and there is no fuzzy drug-name
    matching; unknown names stay only in the raw OCR text. Audio is unchanged.
    """
    image = source == "image"
    splitter = OCR_LINE_SPLIT if image else SENT_SPLIT
    drugs_known = {d["name"].lower() for d in _load_drug_list()}
    alias_to_canonical = {}
    for d in _load_drug_list():
        for a in [d["name"]] + d.get("aliases", []):
            alias_to_canonical[a.lower()] = d["name"].lower()

    drugs, symptoms, vitals, allergies, negations = [], [], [], [], []
    unrecognised = []  # dose mentions whose medicine name is not recognised (review items)
    # cross-sentence join: "paracetamol,\n500 mg" -> "paracetamol 500 mg"
    # also "ibuprofen, for 100 mg" -> "ibuprofen for 100 mg" (second-drug clause)
    if image:  # same join, but never across an OCR line break
        text = re.sub(r"([A-Za-z]+),[ \t]+(?=(?:for\s+)?\d+\s*(?:mg|mcg|g|ml|U)\b)", r"\1 ", text)
    else:
        text = re.sub(r"([A-Za-z]+),\s+(?=(?:for\s+)?\d+\s*(?:mg|mcg|g|ml|U)\b)", r"\1 ", text)
    sents = splitter.split(text)
    # match on digitized copy, keep original wording as proof
    etext = numwords_to_digits(text)
    esents = splitter.split(etext)
    pairs = list(zip(esents, sents)) if len(esents) == len(sents) else [(s, s) for s in sents]
    sents = [p[0] for p in pairs]
    raws = [p[1] for p in pairs]
    diagnosis, followup = {}, {}

    for sent, raw in zip(sents, raws):
        neg = _negated(sent)
        proof_sent = raw.strip()
        # multi-drug split: "amox ... along with ibuprofen ..." -> separate clauses
        # so the second drug's dose/freq is not swallowed by the first row's duration window
        drug_clauses = DRUG_SPLIT_PAT.split(sent) if DRUG_SPLIT_PAT.search(sent) else [sent]
        for clause in drug_clauses:
            for m in DRUG_CTX.finditer(clause):
                rawm = m.group("name").lower()
                if rawm in DOSE_FILLER:  # "thromise in 500 mg", "Tab. X 500 mg": use the word before
                    prev = [w for w in re.findall(r"[A-Za-z]+", clause[:m.start("name")])
                            if w.lower() not in DOSE_FILLER]
                    rawm = prev[-1].lower() if prev else rawm
                canon = alias_to_canonical.get(rawm)
                fuzzy_note = ""
                if canon is None and rawm not in drugs_known:
                    best, score = None, 0
                    if not image:  # OCR: exact drug-list/alias match only, no guessing
                        # fuzzy sound-alike fallback: asitromaisin -> azithromycin (difflib stand-in);
                        # 0.8 keeps genuine misspellings (>=0.82) and rejects different drugs
                        # (e.g. "peridine" -> cetirizine scored 0.71)
                        import difflib
                        for known in list(drugs_known) + list(alias_to_canonical.keys()):
                            sc = difflib.SequenceMatcher(None, rawm, known).ratio()
                            if sc > score:
                                best, score = known, sc
                    if best is not None and score >= FUZZY_MIN:
                        canon = alias_to_canonical.get(best, best)
                        fuzzy_note = f"fuzzy {rawm}->{canon} ({score:.2f})"
                    else:
                        # a dose was stated but the medicine is not recognised: never drop it
                        # silently - keep the words for the clinician (shown as a review item)
                        words = re.findall(r"\S+", clause[:m.start()])[-3:]
                        excerpt = " ".join(words + [m.group(0).strip()]).strip(" ,;")
                        if not neg and not any(u["text"] == excerpt for u in unrecognised):
                            unrecognised.append({"text": excerpt, "source_sentence": proof_sent})
                        continue
                if neg and rawm in ("penicillin", "sulfa", "sulpha"):
                    allergies.append({"text": rawm, "negated": True, "confidence": 0.9,
                                      "source_sentence": proof_sent,
                                      "note": "NEGATED - not added to allergy table"})
                    negations.append({"span": proof_sent[:60], "negated": True})
                    continue
                dose = float(m.group("dose")) if m.group("dose") else None
                raw_unit = (m.group("unit") or "").strip()
                unit = UCUM.get(raw_unit.lower(), raw_unit) or None  # milligram->mg (UCUM)
                freq = (m.group("freq") or "").strip()
                dur = f"{m.group('dur')} {m.group('durunit')}" if m.group("dur") else ""
                if not dose or not unit:
                    conf, color = 0.70, "RED"  # dose/unit missing -> physician must fix
                else:
                    conf = 0.96
                    color = "GREEN"
                if fuzzy_note:
                    conf = 0.88  # fuzzy match -> YELLOW, human must glance
                    color = "YELLOW"
                drugs.append({"name": canon or rawm, "dose": dose, "unit": unit,
                              "frequency": freq, "duration": dur,
                              "confidence": conf, "color": color,
                              "source_sentence": proof_sent, "negated": False,
                              **({"note": fuzzy_note} if fuzzy_note else {})})
        low = sent.lower()
        matched = set()
        for s in SYMPTOMS:
            if s in low:
                # skip substring dup: "pain" inside already-matched "chest pain"
                if any(s in m and s != m for m in matched):
                    continue
                matched.add(s)
                is_neg = neg and s in DENIED_SYMPTOMS
                symptoms.append({"text": s, "confidence": 0.9 if not is_neg else 0.91,
                                 "color": "GREEN" if is_neg else "YELLOW",
                                 "source_sentence": proof_sent,
                                 "negated": is_neg,
                                 **({"note": "DENIED - excluded"} if is_neg else {})})
        # allergies, judged per clause (sentence-level negation flipped
        # "allergic to X, denies Y allergy" into X denied).
        for clause in ALLERGY_CLAUSE_SPLIT.split(sent):
            clow = clause.lower()
            if _negated(clause):
                # standalone denial without dose, e.g. "No penicillin allergy":
                # legacy allowlist first, then generic "allergy to X" candidate
                for aw in ("penicillin", "sulfa", "sulpha"):
                    if aw in clow and not any(a.get("text") == aw and a.get("negated") for a in allergies):
                        allergies.append({"text": aw, "negated": True, "confidence": 0.9,
                                          "source_sentence": proof_sent,
                                          "note": "NEGATED - not added to allergy table"})
                        negations.append({"span": proof_sent[:60], "negated": True})
                for am in ALLERGY_CTX.finditer(clause):
                    cand = (am.group("drug") or "").lower()
                    if not cand or any(a.get("text") == cand and a.get("negated") for a in allergies):
                        continue
                    allergies.append({"text": cand, "negated": True, "confidence": 0.82,
                                      "color": "YELLOW", "source_sentence": proof_sent,
                                      "note": "NEGATED candidate - verify drug name"})
                    negations.append({"span": proof_sent[:60], "negated": True})
                continue
            # documented allergy: only words stated in the source, YELLOW (verify)
            found = []
            for lm in ALLERGY_LIST_PAT.finditer(clause):
                for item in ALLERGY_ITEM_SPLIT.split(lm.group("list")):
                    item = ALLERGY_ITEM_CUT.sub("", item).strip(" -").lower()
                    item = re.sub(r"^(?:a|an|the|any)\s+", "", item)
                    if item and item not in ALLERGY_NONE_WORDS and len(item.split()) <= 3:
                        found.append(item)
            for xm in re.finditer(r"\b([a-z]+)\s+allerg(?:y|ies)\b", clow):
                if xm.group(1) in drugs_known or xm.group(1) in alias_to_canonical \
                        or xm.group(1) in ("penicillin", "sulfa", "sulpha"):
                    found.append(xm.group(1))
            for item in found:
                if not any(a.get("text", "").lower() == item and not a.get("negated") for a in allergies):
                    allergies.append({"text": item, "negated": False, "confidence": 0.85,
                                      "color": "YELLOW", "source_sentence": proof_sent,
                                      "note": "documented allergy - verify"})
        for vm in VITALS_PAT.finditer(sent):
            vitals.append({"text": vm.group(0), "confidence": 0.87, "color": "YELLOW",
                           "source_sentence": proof_sent})
        if NKA_PAT.search(sent):  # explicit "no known allergies" statement
            negations.append(dict(NKA_MARKER))

    # image only: an OCR line with a dose unit that produced no medicine row and no
    # unrecognised-medicine entry (e.g. digits misread: "Nitrofurantoin 1DD mg") is
    # kept as a review item instead of disappearing into the source text
    if image:
        used = [str(d.get("source_sentence", "")) for d in drugs] + \
               [str(u.get("source_sentence", "")) for u in unrecognised]
        for ln in (x.strip() for x in text.split("\n")):
            if ln and re.search(r"\b(?:mg|mcg|ml|units?|tabs?|tablets?|caps?)\b", ln, re.I) \
                    and not any(ln and (ln in u or u in ln) for u in used if u) and not _negated(ln) \
                    and not ALLERGY_LIST_PAT.search(ln):
                unrecognised.append({"text": ln, "source_sentence": ln, "note": "medication line not parsed"})

    # image only: frequency printed alone on the next OCR line -> attach verbatim
    # to the drug above (row YELLOW + note: the clinician checks the pairing)
    if image:
        ocr_lines = [ln.strip() for ln in text.split("\n")]
        for d in drugs:
            proof = str(d.get("source_sentence", "") or "").strip()
            i = next((j for j, ln in enumerate(ocr_lines) if proof and proof in ln), None)
            if d.get("frequency") or i is None or i + 1 >= len(ocr_lines):
                continue
            fm = FREQ_LINE_PAT.match(ocr_lines[i + 1])
            if not fm:
                continue
            d["frequency"] = fm.group("freq")
            if fm.group("dur") and not d.get("duration"):
                d["duration"] = f"{fm.group('dur')} {fm.group('durunit')}"
            d["color"] = "YELLOW"
            d["confidence"] = min(float(d.get("confidence", 0.85) or 0.85), 0.85)
            d["note"] = (str(d.get("note", "")) + " frequency from next OCR line - verify").strip()

    # diagnosis + follow-up sentences (LLM-primary covers more; regex floor)
    # audio: whole text; image: first matching OCR line (no run-on into next line)
    scopes = text.split("\n") if image else [text]
    for scope in scopes:
        m = re.search(r"diagnosis\s*[—–\-:]*\s*([^.]+)", scope, re.I)
        if m and m.group(1).strip():
            diagnosis = {"text": re.sub(r"^(?:is|was|of)\s+", "", m.group(1).strip(), flags=re.I),
                         "icd10": "", "confidence": 0.85,
                         "color": "YELLOW", "source_sentence": m.group(0).strip()}
            break
    for scope in scopes:
        m = re.search(r"(review after[^.]+|follow[- ]?up[^.]+|come back[^.]+|in one week[^.]*|in \d+ (?:days?|weeks?)[^.]*follow[^.]*)", scope, re.I)
        if m:
            followup = {"text": m.group(1).strip(), "confidence": 0.88, "color": "YELLOW",
                        "source_sentence": m.group(0).strip()}
            break

    # image only: labelled patient name + labelled temperature, one OCR line each
    patient = {}
    if image:
        has_temp = any(re.search(r"degree|temp|fahrenheit|°", v["text"], re.I) for v in vitals)
        for line in text.split("\n"):
            pm = IMG_PATIENT_PAT.match(line)
            if not patient and pm:
                name = pm.group("name").strip()
                toks = {t.strip(".'-").lower() for t in name.split()}
                if len(re.sub(r"[^A-Za-z]", "", name)) >= 2 and not toks & IMG_PATIENT_STOP:
                    patient = {"name": name, "confidence": 0.85, "color": "YELLOW",
                               "source_sentence": line.strip(), "note": "labelled OCR line - verify"}
            tm = IMG_TEMP_PAT.search(line)
            if not has_temp and tm:
                lo, hi = IMG_TEMP_RANGE[tm.group("unit").upper()]
                if lo <= float(tm.group("val")) <= hi:  # implausible OCR value -> left in raw text only
                    t = " ".join(tm.group(0).split())
                    if len(t) > 20:  # coords shows vitals text[:20]; keep the unit visible
                        t = f"Temp {tm.group('val')} {tm.group('unit')}"
                    vitals.append({"text": t, "confidence": 0.87, "color": "YELLOW",
                                   "source_sentence": line.strip()})
                    has_temp = True

    # optional spaCy-sm boost (adds no new entities in demo, just confidence nudge)
    try:
        import spacy
        nlp = spacy.load("en_core_web_sm")
        _ = nlp(text[:500])
    except Exception:
        pass
    out = {"drugs": drugs, "symptoms": symptoms, "vitals": vitals,
           "allergies": allergies, "negations": negations,
           "diagnosis": diagnosis, "followup": followup}
    if unrecognised:  # only when present: output shape otherwise unchanged
        out["unrecognised_meds"] = unrecognised
    if patient:  # image source only; audio output shape unchanged
        out["patient"] = patient
    return out


UNITS_OK = {"mg", "mcg", "g", "ml", "U"}


def _merge_tidy_drugs(regex_drugs, tidy_drugs):
    """Per-drug schema merge: tidy fills gaps, never overwrites present values."""
    by_name = {str(d.get("name", "")).lower(): dict(d) for d in regex_drugs if d.get("name")}
    dropped = 0
    for t in tidy_drugs if isinstance(tidy_drugs, list) else []:
        if not isinstance(t, dict) or not t.get("name"):
            dropped += 1
            continue
        from llm_extract import mark_ai_field
        name = str(t["name"]).lower()
        base = by_name.get(name, {"name": t["name"], "confidence": 0.85, "color": "YELLOW",
                                  "source_sentence": "", "negated": False, "ai_suggested": True})
        for k in ("dose", "unit", "frequency", "duration", "confidence"):
            if base.get(k) in (None, "", 0) and t.get(k) not in (None, "", 0):
                base[k] = t[k]
                if k != "confidence":
                    mark_ai_field(base, k)  # AI-filled field: clinician reviews
        try:
            if base.get("dose") is not None:
                base["dose"] = float(base["dose"])
        except (TypeError, ValueError):
            base["dose"] = None
        if base.get("unit") not in UNITS_OK:
            base["unit"] = None
        by_name[name] = base
    return list(by_name.values()), dropped


def ollama_tidy(entities, normalized_en, model="llama3.2:3b", timeout=60):
    """Optional LLM tidy via local Ollama. Per-drug schema merge; falls back to regex."""
    prompt = ("Output strict JSON only with keys drugs[{name,dose,unit,frequency,duration}],"
              "symptoms,vitals,allergies,diagnosis{followup}. Doses numeric + UCUM units mg/mcg/g/ml/U. "
              f"Normalize from: {normalized_en}. Draft: {json.dumps(entities)[:2000]}")
    try:
        out = subprocess.run(["ollama", "run", "--format", "json", model, prompt],
                             capture_output=True, encoding="utf-8", errors="ignore",
                             timeout=timeout)
        txt = (out.stdout or "").strip()
        m = re.search(r"\{.*\}", txt, re.S)
        if m:
            tidy = json.loads(m.group(0))
            if isinstance(tidy, dict) and "drugs" in tidy:
                # per-drug merge: keep regex symptoms/vitals/allergies if model drops them
                merged = dict(entities)
                drugs, dropped = _merge_tidy_drugs(entities.get("drugs", []), tidy.get("drugs"))
                merged["drugs"] = drugs
                from llm_extract import mark_ai_item
                for k in ("symptoms", "vitals", "allergies", "diagnosis", "followup"):
                    if tidy.get(k):
                        merged[k] = ([mark_ai_item(x) for x in tidy[k]] if isinstance(tidy[k], list)
                                     else mark_ai_item(tidy[k]))
                merged["tidy_engine"] = f"ollama:{model}"
                merged["tidy_dropped"] = dropped
                return merged
    except Exception as e:
        entities["tidy_engine"] = f"regex-only (ollama failed: {type(e).__name__})"
        return entities
    entities["tidy_engine"] = "regex-only (ollama skipped/failed)"
    return entities


def run_text_extract(text, segments, job_id="demo-001", use_llm="auto",
                     ollama_model="llama3.2:3b", engine="", source="audio"):
    """Stages 3-4 on already-transcribed text: normalize -> tag -> extract.

    Shared post-text path for audio STT and image OCR.
    source="image": no speech-mishear normalization, no fuzzy drug match, OCR
    lines are boundaries. use_llm=True/"auto" runs the image LLM judge+eval
    (ocr_llm_judge, local Ollama only, gap-fill with verbatim proof); any LLM
    failure falls back to regex-only. Audio path is unchanged.
    `segments` lang tags are refreshed in place, as before.
    Returns (transcript_json, entities_json). entities_json carries "ai_eval"
    (per-field PASS/MISSING/UNPROVEN) when the image LLM path ran.
    """
    norm = normalize_text(text, source=source)
    # refresh segment lang tags with demo LID
    for s in segments:
        s["lang"] = detect_lang_tag(s["text"]) if len(s["text"]) < 200 else norm["lang_tag"]
    ent, llm_reason, ai_eval = None, "", None
    if source == "image" and use_llm in (True, "auto"):
        try:
            from ocr_llm_judge import run_image_llm_eval
            from llm_extract import ollama_available
            ok, why = ollama_available(ollama_model)
            if ok or use_llm is True:
                base = extract_entities(text, norm["normalized_en"], segments, source=source)
                ent, ai_eval = run_image_llm_eval(base, text, segments, model=ollama_model)
            else:
                llm_reason = why
        except ValueError as e:
            ent, llm_reason, ai_eval = None, str(e), None
    elif use_llm in (True, "auto"):
        try:
            from llm_extract import extract_llm_primary, merge_primary, ollama_available
            ok, why = ollama_available(ollama_model)
            if ok or use_llm is True:
                base = extract_entities(text, norm["normalized_en"], segments, source=source)
                llm = extract_llm_primary(text, norm["normalized_en"], segments, model=ollama_model)
                ent = merge_primary(base, llm, model=ollama_model)
            else:
                llm_reason = why
        except ValueError as e:
            ent, llm_reason = None, str(e)
    if ent is None:
        ent = extract_entities(text, norm["normalized_en"], segments, source=source)
        if use_llm is True and source != "image":
            ent = ollama_tidy(ent, norm["normalized_en"], model=ollama_model)
        elif use_llm == "auto" and llm_reason:
            ent["llm_engine"] = f"regex-fallback ({llm_reason})"
    transcript_json = {"job_id": job_id, "text": text, "language": norm["lang_tag"],
                       "segments": segments, "normalized_en": norm["normalized_en"],
                       "normalizations": norm["normalizations"], "stt_engine": engine}
    if source == "image":  # audio output shape stays byte-identical
        transcript_json["source"] = "image"
        if ai_eval is None:
            ent["llm_engine"] = ("regex-only (image source)" if not llm_reason
                                 else f"regex-only (image source, {llm_reason})")
    entities_json = {"job_id": job_id, **ent}
    if ai_eval is not None:
        entities_json["ai_eval"] = ai_eval
    return transcript_json, entities_json


def run_stt_extract(clean_wav, job_id="demo-001", use_llm="auto", model="tiny-int8", ollama_model="llama3.2:3b"):
    stt = transcribe(clean_wav, job_id, model=model)
    return run_text_extract(stt["text"], stt["segments"], job_id, use_llm=use_llm,
                            ollama_model=ollama_model, engine=stt["engine"], source="audio")
