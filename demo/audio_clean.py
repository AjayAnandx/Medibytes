"""Stage 0 Receive + Stage 1 Clean sound - CPU-only investor demo.

Report mapping: PIPELINE_ARCHITECTURE_REPORT.md Sec 4.2 (Stage 0) + 4.3 (Stage 1).
Prod uses ffprobe + Silero VAD + DeepFilterNet GPU. Demo uses stdlib + numpy only,
with optional soundfile / imageio-ffmpeg / noisereduce if installed.
"""
import os
import wave
import contextlib
import shutil

import numpy as np

TARGET_SR = 16000
MAX_BYTES = 100 * 1024 * 1024
MAX_SECONDS = 30 * 60


def _wav_info(path):
    """Return (n_frames, framerate, n_channels, duration_s) for wav, else None."""
    try:
        with contextlib.closing(wave.open(path, "rb")) as w:
            n = w.getnframes()
            sr = w.getframerate()
            ch = w.getnchannels()
            dur = n / float(sr) if sr else 0
            return n, sr, ch, dur
    except Exception:
        return None


def _soundfile_info(path):
    try:
        import soundfile as sf
        info = sf.info(path)
        return info.frames, info.samplerate, info.channels, info.duration
    except Exception:
        return None


def check_audio(path):
    """Stage 0: size + duration + format guard. Raises ValueError(413/422)."""
    if not os.path.isfile(path):
        raise ValueError("404 NOT_FOUND: " + path)
    size = os.path.getsize(path)
    if size > MAX_BYTES:
        raise ValueError(f"413 TOO_BIG: {size} bytes > 100MB")
    if size == 0:
        raise ValueError("422 NO_AUDIO: empty file")

    info = _wav_info(path) or _soundfile_info(path)
    if info is None:
        # Non-wav (mp3 etc.): need imageio-ffmpeg to probe; else accept with unknown duration
        try:
            import imageio_ffmpeg  # noqa: F401
            return {"size": size, "duration": None, "note": "non-wav, will convert via ffmpeg"}
        except Exception:
            # Accept anyway for demo (mock flow); duration checked after convert
            return {"size": size, "duration": None, "note": "non-wav, no ffmpeg - demo accepts"}
    _, _, _, dur = info
    if dur is not None and dur > MAX_SECONDS:
        raise ValueError(f"413 TOO_LONG: {dur:.1f}s is longer than the {MAX_SECONDS / 60:g} min limit")
    if dur is not None and dur < 0.6:
        raise ValueError("422 NO_AUDIO: <0.6s")
    return {"size": size, "duration": dur}


def _load_mono_float(path, target_sr=TARGET_SR, max_seconds=None):
    """Load audio as mono float32 at target_sr. Pure stdlib+numpy for wav; ffmpeg fallback for mp3.

    max_seconds: ffmpeg stops decoding just past this, so an over-long upload
    costs at most limit+1 s of decode (callers compare the decoded length).
    """
    info = _wav_info(path)
    if info is not None:
        _, sr, ch, _ = info
        with contextlib.closing(wave.open(path, "rb")) as w:
            raw = w.readframes(w.getnframes())
            sampwidth = w.getsampwidth()
        dtype = {1: np.int8, 2: np.int16, 4: np.int32}.get(sampwidth, np.int16)
        y = np.frombuffer(raw, dtype=dtype).astype(np.float32)
        if ch > 1:
            y = y.reshape(-1, ch).mean(axis=1)
        # normalize int range to [-1, 1]
        peak = float(np.max(np.abs(y))) if y.size else 0.0
        if peak > 0:
            y = y / (2 ** (8 * sampwidth - 1))
        if sr != target_sr and y.size:
            # linear resample (good enough for demo; prod uses FFmpeg)
            ratio = target_sr / float(sr)
            idx = (np.arange(int(len(y) * ratio)) / ratio).astype(int)
            idx = np.clip(idx, 0, len(y) - 1)
            y = y[idx]
        return y, target_sr

    # Non-wav: try imageio-ffmpeg decode to wav in memory
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        raise ValueError(f"422 DECODE_FAIL: need wav or pip install imageio-ffmpeg ({e})")
    import subprocess
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    limit = ["-t", str(max_seconds + 1)] if max_seconds else []
    try:
        r = subprocess.run(
            [exe, "-y", "-v", "error", "-i", path, *limit, "-ac", "1", "-ar", str(target_sr), tmp],
            capture_output=True, text=True, errors="ignore",
        )
        info = _wav_info(tmp) if r.returncode == 0 else None
        if info is None or info[0] == 0:
            detail = (r.stderr or "").strip().splitlines()[-1:] or ["no audio stream"]
            raise ValueError(f"422 DECODE_FAIL: could not decode audio - file may be corrupt or "
                             f"unsupported ({detail[0][:160]})")
        y, _ = _load_mono_float(tmp, target_sr)
        return y, target_sr
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _save_wav(path, y, sr=TARGET_SR):
    y = np.asarray(y, dtype=np.float32)
    y = np.clip(y, -1.0, 1.0)
    pcm = (y * 32767).astype(np.int16)
    with contextlib.closing(wave.open(path, "wb")) as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def _rms_vad(y, sr, frame_ms=30, thresh=0.02):
    """Tiny energy VAD stand-in for Silero VAD (report Sec 4.3). Returns speech mask + ratio."""
    if y.size == 0:
        return np.zeros(0, dtype=bool), 0.0
    fl = int(sr * frame_ms / 1000)
    n = len(y) // fl
    if n == 0:
        return np.ones(len(y), dtype=bool), 1.0
    frames = y[: n * fl].reshape(n, fl)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    speech_frames = rms >= thresh
    if speech_frames.sum() == 0:
        # adaptive fallback: top-20% energy frames count as speech
        k = max(1, n // 5)
        top = np.argsort(rms)[-k:]
        speech_frames[top] = True
    mask = np.repeat(speech_frames, fl)
    pad = len(y) - len(mask)
    if pad > 0:
        mask = np.concatenate([mask, np.full(pad, speech_frames[-1])])
    return mask, float(speech_frames.mean())


def _dbfs(x):
    """Linear level (1.0 = digital full scale) -> dBFS, 1 dp. Not positive/finite -> None."""
    if isinstance(x, bool) or not isinstance(x, (int, float, np.floating)):
        return None
    x = float(x)
    if not np.isfinite(x) or x <= 0:
        return None
    return round(20 * float(np.log10(x)), 1)


def clean_audio(in_path, out_path, apply_denoise=True):
    """Stage 1: 16k mono + loudnorm-ish + VAD skip + optional noisereduce.

    Returns enhancement_meta dict. Raises 422 NO_AUDIO if >95% silent.
    """
    meta_in = check_audio(in_path)
    y, sr = _load_mono_float(in_path, max_seconds=MAX_SECONDS)
    # authoritative length check on DECODED audio: header metadata can be
    # missing (m4a) or understate the real length (e.g. concatenated mp3)
    dur = len(y) / float(sr) if sr else 0.0
    if dur > MAX_SECONDS:
        raise ValueError(f"413 TOO_LONG: decoded audio is longer than the "
                         f"{MAX_SECONDS / 60:g} min limit (>{MAX_SECONDS}s)")

    # input level measurements (dBFS, before any gain). No noise floor is
    # estimated, so no signal-to-noise ratio is reported.
    rms = float(np.sqrt((y ** 2).mean())) if y.size else 0.0
    in_peak = float(np.max(np.abs(y))) if y.size else 0.0
    clipped = round(float(np.mean(np.abs(y) >= 0.999)), 4) if y.size else None
    # loudnorm-ish: target RMS ~0.1 (prod uses FFmpeg loudnorm I=-16)
    if rms > 1e-6:
        y = y * (0.1 / rms)
        y = np.clip(y, -0.98, 0.98)

    mask, vad_ratio = _rms_vad(y, sr)
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    # digital-silence guard: pure zeros / mic-muted file must stop before STT
    if peak < 0.005:
        raise ValueError("422 NO_AUDIO: digital silence (peak<0.005) - saves GPU, blocks thank-you loop")
    if vad_ratio < 0.05 or y.size < int(sr * 0.6):
        raise ValueError("422 NO_AUDIO: >95% silent or <0.6s speech (saves GPU 4-8x)")

    y_speech = y  # keep full timeline for word timestamps; VAD only reported (prod trims)
    denoise = "none"
    if apply_denoise:
        try:
            import noisereduce as nr
            y_speech = nr.reduce_noise(y=y_speech, sr=sr).astype(np.float32)
            denoise = "noisereduce"
        except Exception as e:  # pure-CPU fallback: gain-normalized only
            denoise = f"not applied ({type(e).__name__}: noisereduce unavailable or failed)"

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    _save_wav(out_path, y_speech, sr)

    return {
        "input": meta_in,
        "sample_rate": sr,
        "channels": 1,
        "duration_s": round(len(y_speech) / sr, 2),
        "vad_ratio": round(vad_ratio, 3),
        "vad_method": "energy: share of 30 ms frames with RMS >= 0.02 after gain (not a speech detector)",
        "input_rms_dbfs": _dbfs(rms),
        "input_peak_dbfs": _dbfs(in_peak),
        "input_clipped_ratio": clipped,
        "quality_note": "level measurements only; no signal-to-noise ratio is measured",
        "denoise": denoise,
        "diarization": "off (default per report)",
    }
