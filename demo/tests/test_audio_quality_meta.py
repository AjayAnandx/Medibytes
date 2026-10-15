"""Regression: audio-quality metadata must describe what is actually measured.

Run:  python -m pytest demo/tests/test_audio_quality_meta.py -q

The old "snr_before_db" was the input RMS level + 60 (identical for a clean
tone and pure noise of the same loudness) and "snr_after_db" was that +6 dB
constant; "denoise" said "noisereduce" even when it was not installed.
Synthetic signals only (sine, white noise, clipped sine, silence).
"""
import math
import os
import sys
import types
import wave

import numpy as np
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO = os.path.dirname(_HERE)
sys.path.insert(0, _DEMO)

import audio_clean  # noqa: E402
import pipeline  # noqa: E402

SR = 16000


def _wav(tmp_path, name, y):
    p = tmp_path / name
    pcm = (np.clip(y, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return str(p)


def _sine(amp, seconds=2.0, f=440):
    t = np.arange(int(SR * seconds)) / SR
    return (amp * np.sin(2 * np.pi * f * t)).astype(np.float32)


def _noise(rms, seconds=2.0):
    return np.random.default_rng(0).normal(0, rms, int(SR * seconds)).astype(np.float32)


@pytest.fixture
def no_noisereduce(monkeypatch):
    monkeypatch.setitem(sys.modules, "noisereduce", None)  # import -> ImportError


def _clean(tmp_path, name, y, **kw):
    return audio_clean.clean_audio(_wav(tmp_path, name, y), str(tmp_path / f"out_{name}"), **kw)


def test_no_field_claims_snr(tmp_path, no_noisereduce):
    tone = _clean(tmp_path, "tone.wav", _sine(0.5))
    noise = _clean(tmp_path, "noise.wav", _noise(0.5 / math.sqrt(2)))
    for meta in (tone, noise):
        assert not [k for k in meta if "snr" in k.lower()], meta
    assert "signal-to-noise" in tone["quality_note"].lower()


def test_level_measurements_are_real_dbfs(tmp_path, no_noisereduce):
    meta = _clean(tmp_path, "tone.wav", _sine(0.5))
    assert meta["input_rms_dbfs"] == pytest.approx(20 * math.log10(0.5 / math.sqrt(2)), abs=0.2)
    assert meta["input_peak_dbfs"] == pytest.approx(20 * math.log10(0.5), abs=0.2)
    assert meta["input_clipped_ratio"] == 0.0
    quiet = _clean(tmp_path, "quiet.wav", _sine(0.05))
    assert quiet["input_rms_dbfs"] < meta["input_rms_dbfs"] - 15  # level, not "quality"


def test_noisy_audio_level_reported_without_quality_claim(tmp_path, no_noisereduce):
    meta = _clean(tmp_path, "noise.wav", _noise(0.1))
    assert meta["input_rms_dbfs"] == pytest.approx(-20.0, abs=0.5)
    assert not [k for k in meta if "snr" in k.lower()]


def test_clipped_input_detected(tmp_path, no_noisereduce):
    meta = _clean(tmp_path, "clipped.wav", _sine(3.0))  # hard-clipped at full scale
    assert meta["input_peak_dbfs"] == pytest.approx(0.0, abs=0.1)
    assert meta["input_clipped_ratio"] > 0.5


def test_silence_still_rejected(tmp_path):
    with pytest.raises(ValueError, match=r"^422 NO_AUDIO"):
        _clean(tmp_path, "silence.wav", np.zeros(SR * 2, dtype=np.float32))


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf"), None, "x"])
def test_dbfs_helper_invalid_values_are_none(bad):
    assert audio_clean._dbfs(bad) is None


def test_dbfs_helper_valid():
    assert audio_clean._dbfs(1.0) == 0.0
    assert audio_clean._dbfs(0.1) == -20.0


def test_denoise_reports_what_actually_ran(tmp_path, monkeypatch):
    y = _sine(0.5)
    monkeypatch.setitem(sys.modules, "noisereduce", None)
    assert _clean(tmp_path, "a.wav", y)["denoise"].startswith("not applied")
    assert _clean(tmp_path, "b.wav", y, apply_denoise=False)["denoise"] == "none"
    fake = types.ModuleType("noisereduce")
    fake.reduce_noise = lambda y, sr: y
    monkeypatch.setitem(sys.modules, "noisereduce", fake)
    assert _clean(tmp_path, "c.wav", y)["denoise"] == "noisereduce"


def test_vad_ratio_is_described_as_energy(tmp_path, no_noisereduce):
    meta = _clean(tmp_path, "tone.wav", _sine(0.5))
    assert 0.0 <= meta["vad_ratio"] <= 1.0
    assert "energy" in meta["vad_method"].lower()


def test_pipeline_cli_prints_level_not_snr(tmp_path, monkeypatch, capsys, no_noisereduce):
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(pipeline, "_HERE", str(out))
    monkeypatch.setattr(sys, "argv", ["pipeline.py", "--in", _wav(tmp_path, "t.wav", _sine(0.5)),
                                      "--key", "demo-001", "--model", "mock", "--no-llm",
                                      "--template", "none"])
    assert pipeline.main() == 0
    line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("[1]"))
    assert "snr" not in line.lower() and "dBFS" in line


# ---- app: legacy cached metadata must not be shown as measured SNR ----

def _app_script():
    import os
    import sys
    sys.path.insert(0, os.environ["MB_TEST_DEMO_DIR"])
    import app
    saved = app._ollama_available
    app._ollama_available = lambda model: (False, "disabled in test")
    try:
        app.main()
    finally:
        app._ollama_available = saved


def test_app_cached_sample_hides_legacy_snr(monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("MB_TEST_DEMO_DIR", _DEMO)
    at = AppTest.from_function(_app_script, default_timeout=120)
    at.run()
    next(s for s in at.selectbox if s.label == "Cached sample").set_value("demo-001 fever (mock)").run()
    assert not at.exception, at.exception
    shown = " ".join(str(j.value) for j in at.json)
    assert "snr" not in shown.lower()
    assert "noisereduce" not in shown  # legacy label is unverifiable -> not displayed as fact
    assert any("older version" in str(c.value) for c in at.caption)
