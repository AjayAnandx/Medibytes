"""Standalone image OCR (Phase 1): .png/.jpg/.jpeg -> extracted text via EasyOCR.

Usage:
  python demo/ocr_extract.py path/to/image.png [--json]

Medical safety: output is exactly what EasyOCR detected. No LLM, no spell or
drug-name correction, no guessing; numbers, doses, units and names are never
altered. Only reading-order grouping (lines) is applied on top of raw detections.
Not yet wired into pipeline.py / app.py / normalize / extract.
"""
import argparse
import json
import os
import sys

SUPPORTED_EXTS = (".png", ".jpg", ".jpeg")
ENGINE = "easyocr"
LANGS = ["en"]

_READER = None  # EasyOCR model load is slow; reuse one reader per process


def _result(source_file, success, text="", blocks=None, lines=None, error_code="", error=""):
    out = {"success": success, "text": text, "engine": ENGINE,
           "source_file": source_file, "blocks": blocks or [], "lines": lines or []}
    if not success:
        out["error_code"] = error_code
        out["error"] = error
    return out


def _load_image(path):
    """Open + fully decode with Pillow -> RGB numpy array. Raises ValueError if unreadable."""
    try:
        import numpy as np
        from PIL import Image, ImageOps, UnidentifiedImageError
    except Exception as e:
        raise RuntimeError(f"Pillow/numpy not available ({type(e).__name__}: {e})")
    try:
        with Image.open(path) as im:
            im.load()  # force full decode so truncated/corrupt files fail here
            im = ImageOps.exif_transpose(im)  # phone photos: honour EXIF rotation
            rgb = im.convert("RGB")
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as e:
        raise ValueError(f"cannot read image ({type(e).__name__}: {e})")
    if rgb.width < 2 or rgb.height < 2:
        raise ValueError(f"image too small ({rgb.width}x{rgb.height})")
    return np.asarray(rgb)


def _get_reader():
    global _READER
    if _READER is None:
        import easyocr
        _READER = easyocr.Reader(LANGS, gpu=False, verbose=False)
    return _READER


def _to_blocks(raw):
    """EasyOCR (bbox, text, conf) tuples -> JSON-safe blocks. Text kept verbatim."""
    blocks = []
    for bbox, text, conf in raw:
        pts = [[int(round(float(x))), int(round(float(y)))] for x, y in bbox]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        blocks.append({"text": str(text), "confidence": round(float(conf), 4), "bbox": pts,
                       "_x0": min(xs), "_y0": min(ys), "_y1": max(ys)})
    return blocks


def _group_lines(blocks):
    """Reading order: top-to-bottom lines, left-to-right within a line.

    Two boxes share a line when their vertical centres are within half the
    smaller box height. Only order/grouping changes; text is never edited.
    """
    order = sorted(range(len(blocks)), key=lambda i: (blocks[i]["_y0"] + blocks[i]["_y1"]) / 2)
    lines = []
    for i in order:
        b = blocks[i]
        cy, h = (b["_y0"] + b["_y1"]) / 2, max(1, b["_y1"] - b["_y0"])
        if lines:
            cur = lines[-1]
            if abs(cy - cur["cy"]) <= min(h, cur["h"]) / 2:
                cur["idx"].append(i)
                n = len(cur["idx"])
                cur["cy"] = (cur["cy"] * (n - 1) + cy) / n
                cur["h"] = min(cur["h"], h)
                continue
        lines.append({"idx": [i], "cy": cy, "h": h})

    out_blocks, out_lines = [], []
    for ln_no, ln in enumerate(lines):
        idx = sorted(ln["idx"], key=lambda i: blocks[i]["_x0"])
        members = []
        for i in idx:
            b = {k: v for k, v in blocks[i].items() if not k.startswith("_")}
            b["line"] = ln_no
            out_blocks.append(b)
            members.append(b)
        out_lines.append({"line": ln_no,
                          "text": " ".join(m["text"] for m in members),
                          "min_confidence": min(m["confidence"] for m in members),
                          "block_count": len(members)})
    return out_blocks, out_lines


def extract_text_from_image(image_path):
    """Image -> OCR text. Never raises for expected input errors; check result['success'].

    Returns {success, text, engine, source_file, blocks[], lines[]} and on failure
    error_code (NOT_FOUND | UNSUPPORTED_EXTENSION | UNREADABLE_IMAGE |
    ENGINE_UNAVAILABLE | ENGINE_FAILURE | NO_TEXT_DETECTED) + error.
    blocks: raw EasyOCR detections {text, confidence, bbox[[x,y]x4], line} in reading order.
    lines: {line, text, min_confidence, block_count}; text = lines joined by newline.
    """
    src = os.path.abspath(str(image_path)) if image_path else ""
    if not image_path or not os.path.isfile(src):
        return _result(src, False, error_code="NOT_FOUND", error=f"file not found: {src}")
    ext = os.path.splitext(src)[1].lower()
    if ext not in SUPPORTED_EXTS:
        return _result(src, False, error_code="UNSUPPORTED_EXTENSION",
                       error=f"unsupported extension '{ext or '(none)'}'; "
                             f"supported: {', '.join(SUPPORTED_EXTS)}")
    try:
        img = _load_image(src)
    except ValueError as e:
        return _result(src, False, error_code="UNREADABLE_IMAGE", error=str(e))
    except RuntimeError as e:
        return _result(src, False, error_code="ENGINE_UNAVAILABLE", error=str(e))

    try:
        reader = _get_reader()
    except ImportError as e:
        return _result(src, False, error_code="ENGINE_UNAVAILABLE",
                       error=f"easyocr not installed ({e}); "
                             f"pip install -r demo/requirements-demo.txt")
    except Exception as e:
        # first run downloads models to ~/.EasyOCR - offline machines fail here
        return _result(src, False, error_code="ENGINE_UNAVAILABLE",
                       error=f"easyocr reader init failed ({type(e).__name__}: {e})")
    try:
        # detail=1 -> (bbox, text, conf); paragraph=False keeps per-box confidence.
        # Greedy decoder, no lexicon/allowlist: text is exactly what the model read.
        raw = reader.readtext(img, detail=1, paragraph=False)
    except Exception as e:
        return _result(src, False, error_code="ENGINE_FAILURE",
                       error=f"easyocr readtext failed ({type(e).__name__}: {e})")

    blocks, lines = _group_lines(_to_blocks(raw))
    blocks = [b for b in blocks if b["text"].strip()]
    lines = [ln for ln in lines if ln["text"].strip()]
    if not lines:
        return _result(src, False, error_code="NO_TEXT_DETECTED",
                       error="OCR ran but detected no text in the image")
    text = "\n".join(ln["text"] for ln in lines)
    return _result(src, True, text=text, blocks=blocks, lines=lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Standalone image OCR (EasyOCR): .png/.jpg/.jpeg")
    ap.add_argument("image_path")
    ap.add_argument("--json", action="store_true", help="print the full result dict as JSON")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows cp1252 console
    except Exception:
        pass

    r = extract_text_from_image(a.image_path)
    print(f"OCR Engine : {r['engine']}")
    print(f"Source File: {r['source_file']}")
    if not r["success"]:
        print(f"Status     : FAILED [{r['error_code']}] {r['error']}")
        if a.json:
            print(json.dumps(r, indent=2, ensure_ascii=False))
        return 1 if r["error_code"] == "NO_TEXT_DETECTED" else 2
    confs = [ln["min_confidence"] for ln in r["lines"]]
    print(f"Status     : OK  lines={len(r['lines'])} blocks={len(r['blocks'])} "
          f"lowest_confidence={min(confs):.2f}")
    print("Extracted Text:")
    print("-" * 60)
    print(r["text"])
    print("-" * 60)
    if a.json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
