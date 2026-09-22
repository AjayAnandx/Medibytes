"""MediBytes one-command demo runner (Windows-safe).

Usage:
  python run_demo.py            # install deps, warm cache (with LLM), launch UI
  python run_demo.py --check    # setup + verify only, no browser launch
  python run_demo.py --no-install  # skip pip install (deps already present)
  python run_demo.py --no-llm-warm  # skip LLM during warm cache (faster startup)

What it does:
  1. Ensures demo/ folders exist
  2. pip installs demo/requirements-demo.txt (unless --no-install)
  3. Warms demo-001/demo-002 cache with --model mock + --use-llm (LLM extraction)
     + rebuilds exports/*.html via --template er_discharge
  4. Launches: streamlit run demo/app.py
"""
import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(ROOT, "demo")
REQ = os.path.join(DEMO, "requirements-demo.txt")


def sh(*args):
    print("+", " ".join(args), flush=True)
    r = subprocess.run(args, cwd=ROOT)
    if r.returncode != 0:
        raise SystemExit(r.returncode)


def ensure_dirs():
    for d in ("audio_in", "cleaned", "transcripts", "entities", "exports", "_state", "templates", "assets"):
        os.makedirs(os.path.join(DEMO, d), exist_ok=True)


def pip_install():
    sh(sys.executable, "-m", "pip", "install", "-q", "-r", REQ)


def warm_cache(use_llm=True):
    # Instant mock runs keep investor story alive even with no mic/model.
    jobs = [("audio_in/sample1_hinglish_fever.wav", "demo-001"),
            ("audio_in/sample2_cough_allergy.wav", "demo-002")]
    for rel, key in jobs:
        src = os.path.join(DEMO, rel)
        if not os.path.isfile(src):
            print(f"skip {key}: missing {rel} (run demo/pipeline.py --gen-samples once)")
            continue
        t = os.path.join(DEMO, "transcripts", f"{key}.json")
        e = os.path.join(DEMO, "entities", f"{key}.entities.json")
        h = os.path.join(DEMO, "exports", f"{key}.html")
        if os.path.isfile(t) and os.path.isfile(e) and os.path.isfile(h):
            print(f"cache hit {key}")
            continue
        # reset idempotency for this key so pipeline rewrites it
        sp = os.path.join(DEMO, "_state", "idempotency.json")
        try:
            s = json.load(open(sp, encoding="utf-8")) if os.path.isfile(sp) else {}
            s.pop(key, None)
            json.dump(s, open(sp, "w", encoding="utf-8"), indent=2)
        except Exception:
            pass
        args = [sys.executable, os.path.join(DEMO, "pipeline.py"), "--in", src,
                "--key", key, "--model", "mock", "--template", "er_discharge"]
        if use_llm:
            args.append("--use-llm")
        sh(*args)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="setup + verify only, do not launch UI")
    ap.add_argument("--no-install", action="store_true")
    ap.add_argument("--no-llm-warm", action="store_true", help="skip LLM extraction during warm cache (faster startup)")
    a = ap.parse_args()

    ensure_dirs()
    if not a.no_install:
        pip_install()
    else:
        print("skip pip install (--no-install)")

    # syntax + import sanity before launch
    import ast
    for f in ("demo/app.py", "demo/pipeline.py", "demo/fill_template.py",
              "demo/stt_extract.py", "demo/audio_clean.py"):
        ast.parse(open(os.path.join(ROOT, f), encoding="utf-8").read())
    print("syntax OK")

    warm_cache(use_llm=not a.no_llm_warm)

    if a.check:
        print("check OK: run `python run_demo.py` to launch the UI")
        return 0

    print("Launching MediBytes UI…")
    sh(sys.executable, "-m", "streamlit", "run", os.path.join(DEMO, "app.py"),
       "--server.headless", "false", "--browser.gatherUsageStats", "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
