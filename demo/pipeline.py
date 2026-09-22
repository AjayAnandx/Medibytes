"""Demo orchestrator Stage 0->4. Sequential, file-based (no Redis/DB).

Usage:
  python pipeline.py --in audio_in/sample1.wav --key demo-001 [--use-llm] [--no-denoise]
  python pipeline.py --gen-samples   # create 2 synthetic wavs for offline testing
"""
import argparse
import hashlib
import json
import os
import sys
import uuid
import wave
import contextlib

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from audio_clean import clean_audio
from stt_extract import run_stt_extract


def _idempotency_get_or_create(key, state_path):
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    state = {}
    if os.path.isfile(state_path):
        try:
            with open(state_path, encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            state = {}
    if key in state:
        return state[key], True
    job_id = key or f"demo-{uuid.uuid4().hex[:6]}"
    state[key or job_id] = job_id
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    return job_id, False


def gen_samples(out_dir):
    """Generate 2 synthetic 8s 16k mono wavs (tone + silence) so pipeline runs with no mic."""
    os.makedirs(out_dir, exist_ok=True)
    sr = 16000
    for name, freq in (("sample1_hinglish_fever.wav", 440), ("sample2_cough_allergy.wav", 520)):
        t = np.arange(sr * 8) / sr
        tone = (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
        tone[int(6 * sr):] = 0.0  # trailing silence shows VAD
        pcm = (np.clip(tone, -1, 1) * 32767).astype(np.int16)
        p = os.path.join(out_dir, name)
        with contextlib.closing(wave.open(p, "wb")) as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm.tobytes())
        print(f"wrote {p}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=None)
    ap.add_argument("--key", default=None)
    ap.add_argument("--use-llm", action="store_true", help="force LLM-primary extraction (fallback regex)")
    ap.add_argument("--no-llm", action="store_true", help="regex only, skip LLM")
    ap.add_argument("--model", default="tiny-int8", help="tiny-int8 (fast) | base-int8 (better) | small-int8 (best CPU) | mock (instant)")
    ap.add_argument("--template", default="er_discharge", help="er_discharge | none")
    ap.add_argument("--no-denoise", action="store_true")
    ap.add_argument("--gen-samples", action="store_true")
    a = ap.parse_args()

    if a.gen_samples:
        gen_samples(os.path.join(_HERE, "audio_in"))
        return 0

    if not a.inp:
        print("Provide --in audio_in/<file>.wav  (or --gen-samples first)", file=sys.stderr)
        return 2

    state_path = os.path.join(_HERE, "_state", "idempotency.json")
    raw_key = a.key or hashlib.sha1(os.path.basename(a.inp).encode()).hexdigest()[:8]
    job_id, dup = _idempotency_get_or_create(raw_key, state_path)

    cleaned = os.path.join(_HERE, "cleaned", f"{job_id}.wav")
    t_path = os.path.join(_HERE, "transcripts", f"{job_id}.json")
    e_path = os.path.join(_HERE, "entities", f"{job_id}.entities.json")

    print(f"[0] job {job_id} duplicate={dup} <- {a.inp}")
    try:
        meta = clean_audio(a.inp, cleaned, apply_denoise=not a.no_denoise)
    except ValueError as e:
        print(f"[{job_id}] {e}", file=sys.stderr)
        return 3
    print(f"[1] cleaned -> {cleaned} vad={meta['vad_ratio']} snr {meta['snr_before_db']}->{meta['snr_after_db']}dB")

    mode = False if a.no_llm else (True if a.use_llm else "auto")
    transcript_json, entities_json = run_stt_extract(cleaned, job_id, use_llm=mode, model=a.model)
    print(f"[2] STT engine={transcript_json['stt_engine']} lang={transcript_json['language']}")
    print(f"[3] normalized: {transcript_json['normalized_en'][:100]}")
    print(f"[4] drugs={len(entities_json['drugs'])} symptoms={len(entities_json['symptoms'])} "
          f"vitals={len(entities_json['vitals'])} allergies={len(entities_json['allergies'])}")

    for p, obj in ((t_path, transcript_json), (e_path, entities_json)):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
    meta_path = os.path.join(_HERE, "cleaned", f"{job_id}.meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    print(f"wrote {t_path}\nwrote {e_path}")

    if a.template != "none":
        try:
            from fill_template import fill_template
            out = fill_template(entities_json, transcript_json, a.template)
            h_path = os.path.join(_HERE, "exports", f"{job_id}.html")
            os.makedirs(os.path.dirname(h_path), exist_ok=True)
            with open(h_path, "w", encoding="utf-8") as f:
                f.write(out["html"])
            print(f"[5] template {a.template} -> {h_path}")
            if out.get("docx_bytes"):
                d_path = os.path.join(_HERE, "exports", f"{job_id}.docx")
                with open(d_path, "wb") as f:
                    f.write(out["docx_bytes"])
                print(f"[5] docx -> {d_path}")
        except Exception as e:
            print(f"[5] template skipped: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
