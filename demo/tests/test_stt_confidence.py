"""Regression: Whisper segment confidence (Bug #4).

Run:  python -m pytest demo/tests/test_stt_confidence.py -q

faster_whisper is replaced by a fake returning segments with chosen
avg_logprob values (synthetic text, no audio decoding, no patient data).
Segment confidence = exp(avg_logprob): Whisper's mean per-token probability,
an uncalibrated model heuristic in [0, 1]. Missing/invalid -> None (never
invented). Higher avg_logprob must never give a lower confidence.
"""
import math
import os
import sys
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO = os.path.dirname(_HERE)
sys.path.insert(0, _DEMO)

import stt_extract  # noqa: E402

_MISSING = object()


def _seg(text, logprob=_MISSING, start=0.0, end=1.0):
    s = types.SimpleNamespace(text=text, start=start, end=end, words=[])
    if logprob is not _MISSING:
        s.avg_logprob = logprob
    return s


def _transcribe(monkeypatch, segments, tmp_path):
    mod = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, *a, **k):
            return iter(segments), types.SimpleNamespace(language="en")

    mod.WhisperModel = WhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    wav = tmp_path / "synthetic.wav"
    wav.write_bytes(b"")
    return stt_extract.transcribe(str(wav), "live-test", model="tiny-int8")


def _confs(out):
    return [s["confidence"] for s in out["segments"]]


def test_low_logprob_gets_low_confidence_high_gets_high(monkeypatch, tmp_path):
    out = _transcribe(monkeypatch, [_seg("Clear test sentence.", -0.05),
                                    _seg("Mumbled test sentence.", -1.5)], tmp_path)
    hi, lo = _confs(out)
    assert hi == round(math.exp(-0.05), 2) == 0.95
    assert lo == round(math.exp(-1.5), 2) == 0.22
    assert hi > lo  # was inverted: -1.5 scored 2.2, -0.05 scored 0.75


@pytest.mark.parametrize("logprob", [0.0, -0.01, -0.6, -1.0, -3.0, -20.0])
def test_confidence_always_within_0_1(monkeypatch, tmp_path, logprob):
    (c,) = _confs(_transcribe(monkeypatch, [_seg("Test sentence.", logprob)], tmp_path))
    assert 0.0 <= c <= 1.0


def test_monotonic_in_logprob(monkeypatch, tmp_path):
    lps = [-4.0, -2.0, -1.0, -0.5, -0.2, 0.0]
    cs = _confs(_transcribe(monkeypatch, [_seg(f"Test {i}.", lp) for i, lp in enumerate(lps)],
                            tmp_path))
    assert cs == sorted(cs)


@pytest.mark.parametrize("bad", [_MISSING, None, float("nan"), float("inf"), float("-inf"),
                                 "not-a-number", 0.5, True])
def test_missing_or_invalid_logprob_is_none_not_invented(monkeypatch, tmp_path, bad):
    (c,) = _confs(_transcribe(monkeypatch, [_seg("Test sentence.", bad)], tmp_path))
    assert c is None  # previously 0.88 (missing) or an out-of-range number


def test_empty_segments(monkeypatch, tmp_path):
    out = _transcribe(monkeypatch, [], tmp_path)
    assert out["segments"] == [] and out["text"] == ""


def test_basis_is_labelled_uncalibrated(monkeypatch, tmp_path):
    (s,) = _transcribe(monkeypatch, [_seg("Test sentence.", -0.3)], tmp_path)["segments"]
    basis = s.get("confidence_basis", "")
    assert "avg_logprob" in basis and "uncalibrated" in basis


def test_thank_you_loop_capped_low_never_raised(monkeypatch, tmp_path):
    out = _transcribe(monkeypatch, [_seg("thank you thank you thank you", -0.01),
                                    _seg("thank you thank you thank you", -3.0)], tmp_path)
    a, b = _confs(out)
    assert a <= 0.2
    assert b == round(math.exp(-3.0), 2)  # already below the cap: not raised to 0.2


def test_mock_segments_unchanged():
    # mock/synthetic path is pinned by IPD ER-baseline hashes; must not change
    segs = stt_extract._mock_segments("One test sentence. Another one.")
    assert [s["confidence"] for s in segs] == [0.9, 0.9]
    assert all("confidence_basis" not in s for s in segs)
