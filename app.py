import os
import json
import base64
from datetime import datetime, timedelta, timezone

import requests
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import streamlit.components.v1 as components

st.set_page_config(layout="wide", page_title="BTC URPD Monitor V49", page_icon="🪙")


def safe_secret(name, default=""):
    """Đọc Streamlit Secrets an toàn; app vẫn chạy khi chưa cấu hình secrets.toml."""
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


# Giao diện dashboard gọn và dễ đọc
st.markdown("""
<style>
.block-container {padding-top: 2rem; padding-bottom: 2rem; max-width: 1500px;}
[data-testid="stMetric"] {
    background: linear-gradient(135deg, rgba(30,41,59,.95), rgba(15,23,42,.95));
    border: 1px solid rgba(148,163,184,.22);
    padding: 18px 20px;
    border-radius: 14px;
    box-shadow: 0 5px 18px rgba(0,0,0,.12);
}
[data-testid="stMetricLabel"] {font-size: .9rem; color: #cbd5e1;}
[data-testid="stMetricValue"] {font-size: 1.65rem;}
section[data-testid="stSidebar"] {
    background: linear-gradient(180deg,#111827 0%,#0f172a 100%);
}
section[data-testid="stSidebar"] * {color: #e5e7eb;}
h1 {letter-spacing: -.035em;}
h2, h3 {letter-spacing: -.02em;}
div[data-testid="stDataFrame"] {border-radius: 12px; overflow: hidden;}
.stButton > button, .stDownloadButton > button {
    border-radius: 9px;
    font-weight: 600;
}
</style>
""", unsafe_allow_html=True)


TOP_START = 85831.0
BOTTOM_START = 58000.0
BOTTOM_END = 78000.0
DEFAULT_ATH = 126198.07
URPD_CHECK_MINUTES = 60
NEWS_CHECK_MINUTES = 12 * 60
URPD_CHECK_SECONDS = URPD_CHECK_MINUTES * 60
NEWS_RELOAD_SECONDS = NEWS_CHECK_MINUTES * 60


def pick_col(df, candidates):
    cols = {str(c).lower().strip().replace(" ", "_"): c for c in df.columns}
    for x in candidates:
        if x in cols:
            return cols[x]
    for c in df.columns:
        k = str(c).lower().strip().replace(" ", "_")
        if any(x in k for x in candidates):
            return c
    return None


def normalize_urpd(raw):
    """Chuẩn hóa URPD từ CSV/XLSX/API BGeometrics."""
    if isinstance(raw, dict):
        for key in ("data", "result", "rows", "values", "urpd", "distribution"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break

    df = pd.DataFrame(raw)
    if df.empty:
        raise ValueError("URPD rỗng.")

    low = pick_col(
        df,
        ["price_low", "low", "min_price", "pricefrom", "price_start", "price"],
    )
    high = pick_col(
        df,
        ["price_high", "high", "max_price", "priceto", "price_end"],
    )
    btc = pick_col(
        df,
        [
