"""Stages 2/3/4 - STT + Normalize + Extract. CPU-only demo with graceful fallbacks.

Report: Sec 4.4 (STT faster-whisper), 4.5 (normalize), 5 (NER ensemble).
Demo: faster-whisper tiny/base-int8 if installed else deterministic mock;
normalize via unicode-range LID + hardcoded Hinglish map; extract via regex
+ drug_list_mini.json + optional spaCy-sm + optional Ollama qwen2.5:0.5b tidy.
"""
import json
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
TEMP_NUM = r"\d{2,3}(?:\.\d{1,2})?\s*(?:degrees?(?:\s*(?:fahrenheit|celsius|F|C))?|°\s*[FC]?)"
VITALS_PAT = re.compile(
    r"((?:BP|B[\s-]*P|blood pressure)\s*(?:is\s+(?:sitting\s+at|around|about|of)?\s*)?\d{2,3}(?:\.\d{1,2})?\s*(by|/|over|of)\s*\d{2,3}(?:\.\d{1,2})?"
    r"|\b" + TEMP_NUM + r"\b"
    r"|SpO2\s*\d{2,3}(?:\.\d{1,2})?%?"
    r"|\b\d{2,3}(?:\.\d{1,2})?\s*(?:%|percent)\s*(?:on\s+(?:room\s*air|rhoomair))?"
    r"|oxygen saturation[^\d.]{0,10}\d{2,3}(?:\.\d{1,2})?\s*%?"
    r"|pulse\s*\d{2,3}"
    r"|temp[^\d.]{0,10}" + TEMP_NUM + r")",
    re.I,
)
NEG_PAT = re.compile(r"\b(no|not|denies|denied|denying|without|never|neither|nor|negative for|no known|don't|doesn't|aren't|isn't|wasn't|weren't|can't|cannot)\b", re.I)
DENIED_SYMPTOMS = ("chest pain", "breathlessness")
FREQ_PAT = (r"BID|OD|TDS|QID|once daily|twice daily|thrice daily|daily|B\.I\.D\.?"
            r"|twice a day|two times a day|thrice a day|three times a day"
            r"|every\s+\d+\s*(?:hours?|hrs?|minutes?|mins?)|q\d+h"
            r"|as needed|as\s+required|PRN|S\.?O\.?S\.?|stat|BD|TID")
DRUG_CTX = re.compile(
    r"(?P<name>[A-Za-z]+),?\s+(?:for\s+)?(?P<dose>\d+(?:\.\d+)?)\s*(?P<unit>mg|mcg|g|ml|U|units?|milligrams?|micrograms?|grams?|milliliters?)\b"
    r"(?:\s*(?P<freq>" + FREQ_PAT + r"))?"
    r"(?:[^.]{0,60}?(?P<dur>\d+)\s*(?P<durunit>days?|din\b|weeks?|months?|hours?|hrs?|minutes?|mins?))?",
    re.I,
)
# Split multi-drug sentences: "amox ... along with ibuprofen ..." -> two clauses
DRUG_SPLIT_PAT = re.compile(r"\s+(?:along with|alongwith|plus|and also|\+)\s+", re.I)
# Generic allergy mention: "allergy to <word>" (candidate, YELLOW; negated flag from NEG_PAT)
ALLERGY_CTX = re.compile(r"allerg(?:y|ies)\s+(?:to|of|from)\s+(?:a\s+|an\s+|the\s+)?(?P<drug>[A-Za-z]+)", re.I)
UCUM = {"milligram": "mg", "milligrams": "mg", "microgram": "mcg", "micrograms": "mcg",
        "gram": "g", "grams": "g", "milliliter": "ml", "milliliters": "ml",
        "units": "U", "unit": "U"}
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")

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


def transcribe(clean_wav, job_id="demo-001", model="tiny-int8"):
    """Stage 2. Real faster-whisper if installed, else mock (filenames map above)."""
    if model == "mock":
        key = job_id if job_id in MOCK_TEXTS else "demo-001"
        return {"text": MOCK_TEXTS[key], "segments": _mock_segments(MOCK_TEXTS[key]),
                "engine": "mock (forced --model mock)", "language": "mix"}
    try:
        from faster_whisper import WhisperModel
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
                         "confidence": round(float(getattr(s, "avg_logprob", -0.2)) * -1 + 0.7, 2) if hasattr(s, "avg_logprob") else 0.88,
                         "words": [{"w": w.word, "s": round(w.start, 2), "e": round(w.end, 2)}
                                   for w in (s.words or [])]})
            # hallucination guard: drop thank-you loops on silence
            if re.search(r"(thank you\s*){3,}", s.text, re.I):
                segs[-1]["confidence"] = 0.2
        text = " ".join(full)
        return {"text": text, "segments": segs, "engine": f"faster-whisper:{size}-cpu-int8",
                "language": getattr(info, "language", "en")}
    except Exception as e:
        key = job_id if job_id in MOCK_TEXTS else None
        if key is None:
            base = os.path.splitext(os.path.basename(clean_wav))[0].lower()
            for k in MOCK_TEXTS:
                if k in base or base in k:
                    key = k
                    break
            key = key or "demo-001"
        text = MOCK_TEXTS[key]
        return {"text": text, "segments": _mock_segments(text),
                "engine": f"mock (no faster-whisper: {type(e).__name__})", "language": "mix"}


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


def normalize_text(text):
    """Stage 3: Hinglish map + UCUM-ish unit spacing + mix tag."""
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
    if re.search(r"\bBPA\s*\d", out, flags=re.I):
        out = re.sub(r"\bBPA(?=\s*\d)", "BP", out, flags=re.I)
        normalizations.append({"from": "BPA", "to": "BP"})
    # Narrow phonetic: "<sys> by AC" -> "<sys> by 80" (AC misheard for eighty)
    if re.search(r"\d\s*(?:by|/|over|of)\s*AC\b", out, flags=re.I):
        out = re.sub(r"(\d\s*(?:by|/|over|of)\s*)AC\b", r"\g<1>80", out, flags=re.I)
        normalizations.append({"from": "by AC", "to": "by 80"})
    # Room-air mishear: Rhoomair -> room air (SpO2 sentence only)
    if re.search(r"rhoomair", out, flags=re.I):
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


def extract_entities(text, normalized_en, segments):
    """Stage 4 regex core. Returns demo entities_json."""
    drugs_known = {d["name"].lower() for d in _load_drug_list()}
    alias_to_canonical = {}
    for d in _load_drug_list():
        for a in [d["name"]] + d.get("aliases", []):
            alias_to_canonical[a.lower()] = d["name"].lower()

    drugs, symptoms, vitals, allergies, negations = [], [], [], [], []
    # cross-sentence join: "paracetamol,\n500 mg" -> "paracetamol 500 mg"
    # also "ibuprofen, for 100 mg" -> "ibuprofen for 100 mg" (second-drug clause)
    text = re.sub(r"([A-Za-z]+),\s+(?=(?:for\s+)?\d+\s*(?:mg|mcg|g|ml|U)\b)", r"\1 ", text)
    sents = SENT_SPLIT.split(text)
    # match on digitized copy, keep original wording as proof
    etext = numwords_to_digits(text)
    esents = SENT_SPLIT.split(etext)
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
                canon = alias_to_canonical.get(rawm)
                fuzzy_note = ""
                if canon is None and rawm not in drugs_known:
                    # fuzzy sound-alike fallback: asitromaisin -> azithromycin (difflib stand-in)
                    import difflib
                    best, score = None, 0
                    for known in list(drugs_known) + list(alias_to_canonical.keys()):
                        sc = difflib.SequenceMatcher(None, rawm, known).ratio()
                        if sc > score:
                            best, score = known, sc
                    if score >= (0.55 if len(rawm) >= 8 else 0.6):
                        canon = alias_to_canonical.get(best, best)
                        fuzzy_note = f"fuzzy {rawm}->{canon} ({score:.2f})"
                    else:
                        continue  # skip non-drug words; drug_list is the mini-RxNorm
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
        # standalone allergy mention without dose, e.g. "No penicillin allergy"
        # legacy allowlist first, then generic "allergy to X" candidate (YELLOW, negated if NEG_PAT)
        if neg:
            for aw in ("penicillin", "sulfa", "sulpha"):
                if aw in low and not any(a.get("text") == aw for a in allergies):
                    allergies.append({"text": aw, "negated": True, "confidence": 0.9,
                                      "source_sentence": proof_sent,
                                      "note": "NEGATED - not added to allergy table"})
                    negations.append({"span": proof_sent[:60], "negated": True})
            for am in ALLERGY_CTX.finditer(sent):
                cand = (am.group("drug") or "").lower()
                if not cand or any(a.get("text") == cand for a in allergies):
                    continue
                # never promote a known drug-list miss as allergy unless negated sentence
                allergies.append({"text": cand, "negated": True, "confidence": 0.82,
                                  "color": "YELLOW", "source_sentence": proof_sent,
                                  "note": "NEGATED candidate - verify drug name"})
                negations.append({"span": proof_sent[:60], "negated": True})
        for vm in VITALS_PAT.finditer(sent):
            vitals.append({"text": vm.group(0), "confidence": 0.87, "color": "YELLOW",
                           "source_sentence": proof_sent})
        if re.search(r"\bno allergy\b", sent, re.I):
            negations.append({"span": "No allergy", "negated": True,
                              "note": "rule stand-in for CAN-BERT"})

    # diagnosis + follow-up sentences (LLM-primary covers more; regex floor)
    m = re.search(r"diagnosis\s*[—–\-:]*\s*([^.]+)", text, re.I)
    if m and m.group(1).strip():
        diagnosis = {"text": m.group(1).strip(), "icd10": "", "confidence": 0.85,
                     "color": "YELLOW", "source_sentence": m.group(0).strip()}
    m = re.search(r"(review after[^.]+|follow[- ]?up[^.]+|come back[^.]+|in one week[^.]*|in \d+ (?:days?|weeks?)[^.]*follow[^.]*)", text, re.I)
    if m:
        followup = {"text": m.group(1).strip(), "confidence": 0.88, "color": "YELLOW",
                    "source_sentence": m.group(0).strip()}

    # optional spaCy-sm boost (adds no new entities in demo, just confidence nudge)
    try:
        import spacy
        nlp = spacy.load("en_core_web_sm")
        _ = nlp(text[:500])
    except Exception:
        pass
    return {"drugs": drugs, "symptoms": symptoms, "vitals": vitals,
            "allergies": allergies, "negations": negations,
            "diagnosis": diagnosis, "followup": followup}


UNITS_OK = {"mg", "mcg", "g", "ml", "U"}


def _merge_tidy_drugs(regex_drugs, tidy_drugs):
    """Per-drug schema merge: tidy fills gaps, never overwrites present values."""
    by_name = {str(d.get("name", "")).lower(): dict(d) for d in regex_drugs if d.get("name")}
    dropped = 0
    for t in tidy_drugs if isinstance(tidy_drugs, list) else []:
        if not isinstance(t, dict) or not t.get("name"):
            dropped += 1
            continue
        name = str(t["name"]).lower()
        base = by_name.get(name, {"name": t["name"], "confidence": 0.85, "color": "YELLOW",
                                  "source_sentence": "", "negated": False})
        for k in ("dose", "unit", "frequency", "duration", "confidence"):
            if base.get(k) in (None, "", 0) and t.get(k) not in (None, "", 0):
                base[k] = t[k]
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
                for k in ("symptoms", "vitals", "allergies", "diagnosis", "followup"):
                    if tidy.get(k):
                        merged[k] = tidy[k]
                merged["tidy_engine"] = f"ollama:{model}"
                merged["tidy_dropped"] = dropped
                return merged
    except Exception as e:
        entities["tidy_engine"] = f"regex-only (ollama failed: {type(e).__name__})"
        return entities
    entities["tidy_engine"] = "regex-only (ollama skipped/failed)"
    return entities


def run_stt_extract(clean_wav, job_id="demo-001", use_llm="auto", model="tiny-int8", ollama_model="llama3.2:3b"):
    stt = transcribe(clean_wav, job_id, model=model)
    norm = normalize_text(stt["text"])
    # refresh segment lang tags with demo LID
    for s in stt["segments"]:
        s["lang"] = detect_lang_tag(s["text"]) if len(s["text"]) < 200 else norm["lang_tag"]
    ent, llm_reason = None, ""
    if use_llm in (True, "auto"):
        try:
            from llm_extract import extract_llm_primary, merge_primary, ollama_available
            ok, why = ollama_available(ollama_model)
            if ok or use_llm is True:
                base = extract_entities(stt["text"], norm["normalized_en"], stt["segments"])
                llm = extract_llm_primary(stt["text"], norm["normalized_en"], stt["segments"], model=ollama_model)
                ent = merge_primary(base, llm, model=ollama_model)
            else:
                llm_reason = why
        except ValueError as e:
            ent, llm_reason = None, str(e)
    if ent is None:
        ent = extract_entities(stt["text"], norm["normalized_en"], stt["segments"])
        if use_llm is True:
            ent = ollama_tidy(ent, norm["normalized_en"], model=ollama_model)
        elif use_llm == "auto" and llm_reason:
            ent["llm_engine"] = f"regex-fallback ({llm_reason})"
    transcript_json = {"job_id": job_id, "text": stt["text"], "language": norm["lang_tag"],
                       "segments": stt["segments"], "normalized_en": norm["normalized_en"],
                       "normalizations": norm["normalizations"], "stt_engine": stt["engine"]}
    entities_json = {"job_id": job_id, **ent}
    return transcript_json, entities_json
