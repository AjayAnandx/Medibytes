# Demo voice scripts — record 20-60s each, close mic, quiet room
# Save as 16k mono wav if possible (phone recorder is fine, pipeline resamples)

## Script 1 — ONE-MINUTE full-English ER dictation (primary investor script)
~140 words, ~60s at dictation pace (~135 wpm). Pause where marked ||.
Covers every template slot: complaint, HPI, vitals, 2 drugs, negated
allergy, denied symptoms, spoken diagnosis, follow-up.

> "Patient has cough for five days, || and fever hundred-and-one since two days. || B-P one-thirty over eighty, || temperature hundred-one degree Fahrenheit, || S-P-O-two ninety-eight percent on room air. || Give azithromycin five-hundred milligram once daily for three days, || and paracetamol five-hundred milligram twice daily for three days. || No penicillin allergy, || patient denies chest pain and breathlessness. || Diagnosis — acute bronchitis. || Review after three days in O-P-D. || Thank you."

Pace: spell drugs slowly (a-zi-thro-my-cin), digits as words
(five-hundred, one-thirty over eighty) — this is how base-int8 hears best.

Slot check: complaint cough/fever | vitals BP 130/80, 101F, SpO2 98 |
drugs azithromycin 500mg OD 3d + paracetamol 500mg BID 3d |
allergies penicillin NEGATED | denied chest pain/breathlessness excluded |
diagnosis acute bronchitis (J-code by physician) | follow-up 3-day OPD.

## Script 2 — Fever happy path, Hinglish (job demo-001, mock)
> "Patient ko fever hai, bukhar 101 degree, give paracetamol 500 mg BID, do time, 3 din tak. No allergy."

## Script 3 — Cough + negation + vitals, Hinglish (job demo-002, mock)
> "Khansi hai, cough for 5 days, BP 130 by 80, give azithromycin 500 mg once daily 3 days. No penicillin allergy, denies chest pain."

## If no mic: use cached outputs
transcripts/demo-001.json and entities/demo-001.entities.json are pre-generated
so investor sees full flow even if live STT is slow.
