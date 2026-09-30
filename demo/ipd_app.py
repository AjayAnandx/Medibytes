"""MediBytes IPD P1 standalone demo (typed ward round -> progress note).

Run from the repository root:
    streamlit run demo/ipd_app.py

Database: MEDIBYTES_IPD_DB, else <app-data or temp>/MediBytes/ipd_demo.sqlite
(never inside the repository). Independent of demo/app.py (ER demo).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import streamlit as st  # noqa: E402

from ipd.ui import render_app  # noqa: E402

st.set_page_config(page_title="MediBytes IPD — P1 demo", layout="wide")
render_app(st)
