"""
BTC URPD Monitor V75
====================

Một file, hai chế độ chạy:

  • Dashboard (Streamlit Cloud):   streamlit run app.py
      → CHỈ ĐỌC dữ liệu đã lưu trong repo (urpd_history.json, ...).
        Giá BTC, vốn hóa, tin tức, lịch kinh tế vẫn lấy trực tiếp.

  • Cập nhật dữ liệu (GitHub Actions):   python app.py --update-data
      → Lấy URPD (Bitview), Supply in Loss + giá vốn STH/LTH (ResearchBitcoin),
        Realized P/L + MVRV/NUPL/SOPR (BGeometrics), dòng tiền ETF (SoSoValue/Farside), phái sinh BTC (OKX),
        stablecoin (DeFiLlama), Coinbase Premium, Sợ hãi & Tham lam, ghi vào các file JSON,
        gửi tóm tắt Telegram (nếu có cấu hình). Workflow sẽ commit các file này.

Secrets/biến môi trường:
  Streamlit:  GITHUB_REPO (vd "vietquang1205/bitcoin-urpd-dashboard"), GITHUB_BRANCH (mặc định "main"),
              GITHUB_TOKEN (tùy chọn, chỉ cần quyền đọc nếu repo private).
  Actions:    RESEARCHBITCOIN_API_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (tùy chọn),
              TELEGRAM_LOSS_ALERT_PCT (mặc định 50), RB_BACKFILL_DAYS (mặc định 90),
              SOSOVALUE_API_KEY / ETF_FLOWS_API_KEY (tùy chọn — nguồn dòng tiền ETF ổn định hơn Farside).
"""
import os
import sys
import re
import json
import glob
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html import unescape
from concurrent.futures import ThreadPoolExecutor

import requests
import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots

APP_VERSION = "V75"
UPDATE_MODE = "--update-data" in sys.argv

URPD_FILE = "urpd_history.json"
RESEARCH_FILE = "researchbitcoin_daily_history.json"
REALIZED_FILE = "bgeometrics_realized_pl_history.json"
ONCHAIN_FILE = "bgeometrics_onchain_history.json"
STATUS_FILE = "update_status.json"

DEFAULT_ATH = 126198.07
HISTORY_MAX_DAYS = 365
DAILY_MAX_DAYS = 730
URPD_INITIAL_DAYS = 90          # lần đầu chạy updater: lấy 90 ngày URPD để có heatmap/xu hướng
URPD_BACKFILL_PER_RUN = 40      # mỗi lần chạy bù thêm tối đa 40 ngày cũ cho tới khi đủ 365 ngày
WATCH_ZONE_DEFAULT = (57800.0, 67300.0)   # vùng theo dõi mặc định (vùng đáy) — đổi được ngay trên app
BG_INITIAL_DAYS = 365           # lần đầu: Realized P/L + MVRV/NUPL/SOPR 365 ngày (1 request/chỉ số)
NEAR_PCT = 0.10                 # hỗ trợ/cản gần = ±10% quanh giá
VN_TZ = timezone(timedelta(hours=7))
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 BTC-URPD-Dashboard/70", "Accept": "application/json"}


# =============================================================================
# TIỆN ÍCH CHUNG
# =============================================================================
def utc_now():
    return datetime.now(timezone.utc)


def utc_today():
    return utc_now().date()


def day_str(d):
    return d.strftime("%Y-%m-%d")


def parse_day(text):
    return datetime.strptime(str(text)[:10], "%Y-%m-%d").date()


def is_day_key(k):
    try:
        parse_day(k)
        return isinstance(k, str) and len(k) == 10
    except Exception:
        return False


def fmt_btc(x, signed=False, digits=0):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "N/A"
    return f"{x:+,.{digits}f} BTC" if signed else f"{x:,.{digits}f} BTC"


def fmt_usd(x, signed=False, digits=0):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "N/A"
    if signed:
        return f"{'+' if x >= 0 else '−'}${abs(x):,.{digits}f}"
    return f"${x:,.{digits}f}"


def fmt_big_usd(x, signed=False):
    if x is None or not np.isfinite(x):
        return "N/A"
    sign = ("+" if x >= 0 else "−") if signed else ("−" if x < 0 else "")
    a = abs(x)
    if a >= 1e12:
        return f"{sign}${a / 1e12:.2f}T"
    if a >= 1e9:
        return f"{sign}${a / 1e9:.2f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:.1f}M"
    return f"{sign}${a:,.0f}"


def safe_float(x):
    try:
        v = float(x)
        return v if np.isfinite(v) else None
    except (TypeError, ValueError):
        return None


# =============================================================================
# CHUẨN HÓA URPD
# =============================================================================
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
    """Chuẩn hóa URPD từ CSV/XLSX/API về 3 cột price_low, price_high, btc_amount."""
    if isinstance(raw, dict):
        for key in ("data", "result", "rows", "values", "urpd", "distribution"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break

    df = pd.DataFrame(raw)
    if df.empty:
        raise ValueError("URPD rỗng.")

    low = pick_col(df, ["price_low", "pricelower", "price_lower", "low", "min_price", "pricefrom", "price_start", "price"])
    high = pick_col(df, ["price_high", "priceupper", "price_upper", "high", "upper", "max_price", "priceto", "price_end"])
    btc = pick_col(df, ["btc_amount", "btc", "amount", "supply", "realized_supply", "quantity", "btcsupply"])
    if low is None or btc is None:
        raise ValueError(f"Không nhận diện được cột URPD: {list(df.columns)}")

    out = pd.DataFrame({
        "price_low": pd.to_numeric(df[low], errors="coerce"),
        "btc_amount": pd.to_numeric(df[btc], errors="coerce"),
    })
    out["price_high"] = pd.to_numeric(df[high], errors="coerce") if high is not None else out["price_low"]
    out = out.replace([np.inf, -np.inf], np.nan).dropna()
    out = out[out.btc_amount >= 0].copy()

    # Tổng cung BTC < 21 triệu; tổng > 22 triệu nghĩa là đơn vị satoshi.
    if not out.empty and out.btc_amount.sum() > 2.2e7:
        out["btc_amount"] = out["btc_amount"] / 100_000_000

    # API chỉ trả một mức giá cho mỗi bucket → dựng biên bucket.
    if (out.price_low == out.price_high).all() and len(out) > 1:
        out = out.sort_values("price_low").reset_index(drop=True)
        p = out.price_low.to_numpy(float)
        e = np.empty(len(p) + 1)
        e[1:-1] = (p[:-1] + p[1:]) / 2
        e[0] = max(0.0, p[0] - (e[1] - p[0]))
        e[-1] = p[-1] + (p[-1] - e[-2])
        out["price_low"], out["price_high"] = e[:-1], e[1:]

    out = out[out.price_high > out.price_low]
    if out.empty:
        raise ValueError("Không có bucket URPD hợp lệ.")
    return out.sort_values("price_low").reset_index(drop=True)[["price_low", "price_high", "btc_amount"]]


def btc_in_range(df, low, high):
    """Lượng BTC có giá vốn trong [low, high]; bucket cắt ngang biên được nội suy theo tỷ lệ."""
    if df is None or df.empty or high <= low:
        return 0.0
    lo_arr = df.price_low.to_numpy(float)
    hi_arr = df.price_high.to_numpy(float)
    width = hi_arr - lo_arr
    overlap = np.maximum(0.0, np.minimum(hi_arr, high) - np.maximum(lo_arr, low))
    return float(np.sum(df.btc_amount.to_numpy(float) * overlap / width))


def urpd_to_records(df):
    return [
        {"price_low": round(float(a), 2), "price_high": round(float(b), 2), "btc_amount": round(float(c), 8)}
        for a, b, c in zip(df.price_low, df.price_high, df.btc_amount)
    ]


def records_to_urpd(records):
    return normalize_urpd(pd.DataFrame(records))


def snapshot_changed(old, new):
    """So sánh dữ liệu gốc (bucket + giá) để quyết định có cần ghi lại hay không."""
    if not isinstance(old, dict) or not old.get("urpd"):
        return True
    op, np_ = safe_float(old.get("price")), safe_float(new.get("price"))
    if (op is None) != (np_ is None) or (op is not None and abs(op - np_) > 1e-6):
        return True
    try:
        a = records_to_urpd(old["urpd"]).to_numpy(float)
        b = records_to_urpd(new["urpd"]).to_numpy(float)
    except Exception:
        return True
    return a.shape != b.shape or not np.allclose(a, b, rtol=0, atol=1e-5)


# =============================================================================
# NGUỒN DỮ LIỆU: BITVIEW (URPD)
# =============================================================================
def _bitview_bucket_rows(buckets):
    """Đọc buckets của Bitview ở cả 2 định dạng:
    • cũ: [{"price_floor": 1000, "supply": 123.4}, ...]
    • mới (từ 31/10/2026): {"price": [0, 1000, ...], "supply": [...], "realized_cap": [...], ...}"""
    pairs = []
    if isinstance(buckets, dict):
        prices = buckets.get("price") or buckets.get("price_floor") or []
        supplies = buckets.get("supply") or []
        pairs = list(zip(prices, supplies))
    elif isinstance(buckets, list):
        for b in buckets:
            if isinstance(b, dict):
                pairs.append((b.get("price_floor", b.get("price")), b.get("supply")))
            elif isinstance(b, (list, tuple)) and len(b) >= 2:
                pairs.append((b[0], b[1]))
    out = []
    for p, v in pairs:
        p, v = safe_float(p), safe_float(v)
        if p is not None and v is not None and v >= 0:
            out.append((p, v))
    return out


def fetch_bitview_urpd(day, agg="lin1000", cohort="all"):
    """URPD từ Bitview / Bitcoin Research Kit. Trả (frame, url, close, total_supply).
    Luôn quy về bucket cố định $1.000 để khớp với history cũ."""
    base_url = f"https://bitview.space/api/urpd/{cohort}/{day}"
    # Bitview đổi API ngày 31/10/2026: tham số weight=raw bị từ chối (lỗi 400) → thử agg=lin1000 trước.
    attempts = [{"agg": agg}, {"agg": agg, "weight": "raw"}, {}]
    errors, response = [], None
    for params in attempts:
        try:
            r = requests.get(base_url, params=params, headers=HTTP_HEADERS, timeout=30)
            if r.status_code == 404:
                errors.append("404: chưa có snapshot")
                continue
            r.raise_for_status()
            payload = r.json()
            rows = _bitview_bucket_rows(payload.get("buckets")) if isinstance(payload, dict) else []
            if not rows:
                raise ValueError("response không có buckets hợp lệ")
            response = (r, payload, rows)
            break
        except Exception as e:
            errors.append(f"{params or 'mặc định'} → {type(e).__name__}: {str(e)[:120]}")
    if response is None:
        raise RuntimeError(f"Bitview không lấy được {day}. " + " | ".join(errors))

    r, payload, rows = response
    # Gộp về bucket $1.000 (nếu API trả dạng chi tiết/raw thì mỗi mức giá được dồn vào bucket chứa nó).
    agg_map = {}
    for p, v in rows:
        low = float(np.floor(p / 1000.0) * 1000.0)
        agg_map[low] = agg_map.get(low, 0.0) + v
    frame = pd.DataFrame([{"price_low": k, "price_high": k + 1000.0, "btc_amount": v}
                          for k, v in sorted(agg_map.items())])
    out = normalize_urpd(frame)
    total_supply = payload.get("total_supply", payload.get("supply"))
    return out, r.url, payload.get("close"), total_supply if not isinstance(total_supply, (dict, list)) else None


def fetch_bgeometrics_urpd(day):
    """URPD dự phòng từ BGeometrics (priceLower, priceUpper, btcSupply)."""
    r = requests.get("https://bitcoin-data.com/v1/urpd", params={"day": day},
                     headers={"Accept": "application/hal+json", **HTTP_HEADERS}, timeout=30)
    if r.status_code == 429:
        raise RuntimeError("BGeometrics đang giới hạn lượt gọi.")
    r.raise_for_status()
    payload = r.json()
    if isinstance(payload, dict):
        for key in ("data", "result", "rows", "values"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"BGeometrics không trả URPD cho ngày {day}.")
    return normalize_urpd(payload), r.url


# =============================================================================
# NGUỒN DỮ LIỆU: GIÁ & VỐN HÓA (trực tiếp)
# =============================================================================
def fetch_live_price():
    for url in ("https://api.coinbase.com/v2/prices/BTC-USD/spot",
                "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"):
        try:
            j = requests.get(url, headers=HTTP_HEADERS, timeout=15).json()
            if "bitcoin" in j:
                return float(j["bitcoin"]["usd"])
            return float(j["data"]["amount"])
        except Exception:
            continue
    return None


def fetch_market_overview():
    """Vốn hóa toàn thị trường, BTC dominance loại USDT+USDC, altcoin (CoinGecko)."""
    g = requests.get("https://api.coingecko.com/api/v3/global", headers=HTTP_HEADERS, timeout=20)
    g.raise_for_status()
    g = g.json().get("data", {})
    total = float(g.get("total_market_cap", {}).get("usd"))
    total_chg = safe_float(g.get("market_cap_change_percentage_24h_usd"))
    r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                     params={"ids": "bitcoin,tether,usd-coin", "vs_currencies": "usd",
                             "include_market_cap": "true", "include_24hr_change": "true"},
                     headers=HTTP_HEADERS, timeout=20)
    r.raise_for_status()
    d = r.json()
    btc = float(d["bitcoin"]["usd_market_cap"])
    usdt = float(d["tether"]["usd_market_cap"])
    usdc = float(d["usd-coin"]["usd_market_cap"])
    btc_chg = safe_float(d["bitcoin"].get("usd_24h_change"))

    def prev(cur, pct):
        pct = safe_float(pct)
        return cur / (1 + pct / 100) if pct is not None and pct > -100 else None

    adj_total = total - usdt - usdc
    alt = total - btc - usdt - usdc
    parts = [prev(total, total_chg), prev(btc, btc_chg),
             prev(usdt, d["tether"].get("usd_24h_change")), prev(usdc, d["usd-coin"].get("usd_24h_change"))]
    alt_chg = None
    if all(p is not None for p in parts):
        alt_prev = parts[0] - parts[1] - parts[2] - parts[3]
        if alt_prev > 0:
            alt_chg = (alt / alt_prev - 1) * 100
    return {
        "total_mcap": total, "total_mcap_change_24h": total_chg,
        "btc_mcap": btc, "btc_change_24h": btc_chg,
        "usdt_usdc": usdt + usdc,
        "adjusted_btc_dom": btc / adj_total * 100 if adj_total > 0 else None,
        "alt_ex_stables": alt, "alt_change_24h": alt_chg,
    }


# =============================================================================
# NGUỒN DỮ LIỆU: RESEARCHBITCOIN
# =============================================================================
RESEARCH_METRICS = {
    # slug API           : khóa lưu trong file
    "supply_in_loss": "supply_in_loss",
    "supply_in_loss_percent": "supply_in_loss_percent",
    "realized_price_sth": "sth_realized_price",
    "realized_price_lth": "lth_realized_price",
}


def _research_endpoint(metric_slug):
    return {
        "supply_in_loss": ("supply_in_profitloss", "supply_in_loss"),
        "supply_in_loss_percent": ("supply_in_profitloss", "supply_in_loss_percent"),
        "realized_price_lth": ("realizedprice", "realized_price_lth"),
        "realized_price_sth": ("realizedprice", "realized_price_sth"),
    }.get(metric_slug, (metric_slug, metric_slug))


def _parse_time_value(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        x = float(value)
        if x > 1e14:
            x /= 1e6
        elif x > 1e11:
            x /= 1e3
        try:
            return datetime.fromtimestamp(x, tz=timezone.utc).date()
        except Exception:
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).date()
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except Exception:
            pass
    return None


def parse_daily_series(payload, value_keys, time_keys=("timestamp_ms", "timestamp", "time", "date", "datetime",
                                                       "d", "day", "unixTs", "t")):
    """Đọc nhiều dạng JSON time-series về DataFrame date/value."""
    rows = payload
    if isinstance(payload, dict):
        rows = []
        for key in ("data", "result", "rows", "values", "results", "series"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
        else:
            if payload and all(not isinstance(v, (dict, list)) for v in payload.values()):
                rows = [{"time": k, "value": v} for k, v in payload.items()]
    out = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            d, value = _parse_time_value(row[0]), row[1]
        elif isinstance(row, dict):
            d = None
            for key in time_keys:
                if key in row:
                    d = _parse_time_value(row.get(key))
                    if d is not None:
                        break
            value = None
            for key in value_keys:
                if row.get(key) is not None:
                    value = row.get(key)
                    break
            if value is None:  # fallback: field số đầu tiên không phải thời gian
                for key, cand in row.items():
                    if key in time_keys:
                        continue
                    if safe_float(cand) is not None:
                        value = cand
                        break
        else:
            continue
        v = safe_float(value)
        if d is not None and v is not None:
            out.append({"date": d, "value": v})
    if not out:
        raise ValueError("Không đọc được time-series.")
    return pd.DataFrame(out).drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)


def _researchbitcoin_error(r, metric_slug):
    try:
        detail = r.json()
    except Exception:
        detail = r.text[:500]
    msg = {400: "tham số không hợp lệ", 401: "token không hợp lệ/hết hạn", 402: "hết quota",
           403: "token không có quyền", 429: "bị giới hạn tốc độ"}.get(r.status_code, "lỗi")
    return RuntimeError(f"ResearchBitcoin {r.status_code} — {msg} ({metric_slug}): {detail}")


def researchbitcoin_fetch_series(token, metric_slug, from_date, to_date):
    group, field = _research_endpoint(metric_slug)
    r = requests.get(
        f"https://api.researchbitcoin.net/v2/{group}/{field}",
        headers={"Accept": "application/json", "X-API-Token": token.strip()},
        params={"resolution": "d1", "output_format": "json",
                "from_time": from_date.isoformat(), "to_time": to_date.isoformat()},
        timeout=30,
    )
    if r.status_code >= 400:
        raise _researchbitcoin_error(r, metric_slug)
    return parse_daily_series(r.json(), [field, metric_slug, "value", "val", "metric_value"])


def researchbitcoin_quota(token):
    """Đọc quota còn lại (DP); None nếu không đọc được."""
    try:
        r = requests.get("https://api.researchbitcoin.net/v2/info/user_info",
                         headers={"Accept": "application/json", "X-API-Token": token.strip()}, timeout=20)
        if r.status_code >= 400:
            return None
        wanted = {"quota_remaining", "remaining_quota", "quota_left", "remaining"}

        def find(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if str(k).lower() in wanted and safe_float(v) is not None:
                        return float(v)
                for v in obj.values():
                    f = find(v)
                    if f is not None:
                        return f
            elif isinstance(obj, list):
                for v in obj:
                    f = find(v)
                    if f is not None:
                        return f
            return None
        return find(r.json())
    except Exception:
        return None


# =============================================================================
# NGUỒN DỮ LIỆU: BGEOMETRICS (miễn phí)
# =============================================================================
BG_VALUE_KEYS = {
    "realized-profit": ["realizedProfit", "realized_profit", "realized-profit", "profit"],
    "realized-loss": ["realizedLoss", "realized_loss", "realized-loss", "loss"],
    "btc-price": ["btcPrice", "btc_price", "btc-price", "price", "close"],
    "mvrv": ["mvrv", "MVRV", "value"],
    "nupl": ["nupl", "NUPL", "value"],
    "sopr": ["sopr", "SOPR", "value"],
    "sth-realized-price": ["sthRealizedPrice", "sth_realized_price", "realizedPrice", "value"],
    "lth-realized-price": ["lthRealizedPrice", "lth_realized_price", "realizedPrice", "value"],
}


def bgeometrics_fetch_daily(slug, start_date, end_date):
    r = requests.get(f"https://bitcoin-data.com/v1/{slug}",
                     params={"startday": start_date.isoformat(), "endday": end_date.isoformat()},
                     headers=HTTP_HEADERS, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"BGeometrics {slug} HTTP {r.status_code}: {r.text[:200]}")
    return parse_daily_series(r.json(), BG_VALUE_KEYS.get(slug, ["value"]))


# =============================================================================
# PHÂN TÍCH (hàm thuần, dùng chung cho dashboard và Telegram)
# =============================================================================
def snapshot_metrics(frame, price, ath):
    """Các con số chính của một snapshot URPD tại một mức giá tham chiếu."""
    total = float(frame.btc_amount.sum())
    return {
        "total": total,
        "below": btc_in_range(frame, 0.0, price),
        "overhead": btc_in_range(frame, price, max(float(ath), price)),
        "support_near": btc_in_range(frame, price * (1 - NEAR_PCT), price),
        "resistance_near": btc_in_range(frame, price, price * (1 + NEAR_PCT)),
    }


def rebin_fixed(frame, bin_size=1000.0, max_price=None):
    """Quy URPD về các bin cố định (nội suy theo phần giao nhau)."""
    max_price = float(max_price or frame.price_high.max())
    n_bins = max(1, int(np.ceil(max_price / bin_size)))
    edges = np.arange(n_bins + 1, dtype=float) * bin_size
    amounts = np.zeros(n_bins)
    for low, high, btc in zip(frame.price_low.to_numpy(float), frame.price_high.to_numpy(float),
                              frame.btc_amount.to_numpy(float)):
        low, high = max(low, 0.0), min(high, edges[-1])
        width = high - low
        if width <= 0 or btc <= 0:
            continue
        first = int(low // bin_size)
        last = min(n_bins - 1, int(np.nextafter(high, -np.inf) // bin_size))
        for i in range(first, last + 1):
            ov = min(high, edges[i + 1]) - max(low, edges[i])
            if ov > 0:
                amounts[i] += btc * ov / width
    return edges, amounts


def find_sr_zones(frame, price, n=3, window=0.35, zone_bins=3, bin_size=1000.0):
    """Vùng hỗ trợ/kháng cự tự động = các cụm giá vốn dày nhất (rộng zone_bins × $1.000)
    nằm dưới/trên giá trong phạm vi ±window. Trả (supports, resistances) — gần giá nhất trước."""
    if frame is None or frame.empty or not price:
        return [], []
    edges, amt = rebin_fixed(frame, bin_size, max_price=price * (1 + window) + bin_size * (zone_bins + 1))
    if len(amt) < zone_bins:
        return [], []
    sums = np.convolve(amt, np.ones(zone_bins), "valid")
    lows = edges[:len(sums)]
    highs = lows + zone_bins * bin_size
    total = float(frame.btc_amount.sum()) or 1.0

    def pick(mask):
        idx = np.where(mask)[0]
        chosen = []
        for i in idx[np.argsort(sums[idx])[::-1]]:
            if sums[i] <= 0:
                break
            if all(abs(i - j) >= zone_bins for j in chosen):
                chosen.append(i)
            if len(chosen) == n:
                break
        return [{
            "low": float(lows[i]), "high": float(highs[i]), "btc": float(sums[i]),
            "share_pct": float(sums[i]) / total * 100,
            "distance_pct": ((lows[i] + highs[i]) / 2 / price - 1) * 100,
        } for i in chosen]

    supports = pick((highs <= price) & (lows >= price * (1 - window)))
    resistances = pick((lows >= price) & (highs <= price * (1 + window)))
    supports.sort(key=lambda z: -z["high"])
    resistances.sort(key=lambda z: z["low"])
    return supports, resistances


def bucket_deltas(current_df, old_df):
    """So sánh bucket hai snapshot; supply cũ được nội suy vào bucket hiện tại theo phần giao nhau."""
    if current_df is None or old_df is None or current_df.empty or old_df.empty:
        return pd.DataFrame()
    cur = current_df[current_df.price_high > 1].sort_values("price_low")
    old = old_df[old_df.price_high > 1].sort_values("price_low")
    o_lo, o_hi = old.price_low.to_numpy(float), old.price_high.to_numpy(float)
    o_amt, o_w = old.btc_amount.to_numpy(float), (o_hi - o_lo)
    rows, i, n = [], 0, len(old)
    for lo, hi, amount in zip(cur.price_low.to_numpy(float), cur.price_high.to_numpy(float),
                              cur.btc_amount.to_numpy(float)):
        while i < n and o_hi[i] <= lo:
            i += 1
        prev, j = 0.0, i
        while j < n and o_lo[j] < hi:
            ov = min(o_hi[j], hi) - max(o_lo[j], lo)
            if ov > 0 and o_w[j] > 0:
                prev += o_amt[j] * ov / o_w[j]
            j += 1
        delta = amount - prev
        rows.append({"low": lo, "high": hi, "current": amount, "old": prev, "delta": delta,
                     "pct": delta / prev * 100 if prev > 0 else np.nan})
    return pd.DataFrame(rows)


def light(value, good_above=None, bad_below=None, good_below=None, bad_above=None):
    """Đèn tín hiệu 🟢/🟡/🔴 theo ngưỡng."""
    if value is None or not np.isfinite(value):
        return "⚪"
    if good_above is not None and bad_below is not None:
        return "🟢" if value > good_above else ("🔴" if value < bad_below else "🟡")
    if good_below is not None and bad_above is not None:
        return "🟢" if value < good_below else ("🔴" if value > bad_above else "🟡")
    return "⚪"


def latest_value(df, col, on_or_before=None):
    if df is None or df.empty or col not in df.columns:
        return None, None
    s = df[["date", col]].dropna()
    if on_or_before is not None:
        s = s[s["date"] <= on_or_before]
    if s.empty:
        return None, None
    row = s.iloc[-1]
    return float(row[col]), row["date"]


def value_days_before(df, col, ref_date, days):
    """Giá trị gần nhất ở/trước (ref_date - days)."""
    if ref_date is None:
        return None
    v, _ = latest_value(df, col, on_or_before=ref_date - timedelta(days=days))
    return v


def build_signals(price, onchain_df, realized_df, cur_frame, frame_7d_ago, ath, etf=None):
    """Bảng đèn tín hiệu — thay cho 4 điểm số cũ. Mỗi dòng: tín hiệu, đèn, giá trị, cách đọc."""
    rows = []
    sth, _ = latest_value(onchain_df, "sth_realized_price")
    lth, _ = latest_value(onchain_df, "lth_realized_price")
    if sth and price:
        d = (price / sth - 1) * 100
        rows.append(["Giá so với giá vốn STH (holder < 155 ngày)", light(d, good_above=3, bad_below=-3),
                     f"{d:+.1f}% (STH {fmt_usd(sth)})",
                     "Trên STH: holder mới đang lãi, thường là pha khỏe. Dưới STH: holder mới đang lỗ, dễ bán khi hồi về hòa vốn."])
    if lth and price:
        d = (price / lth - 1) * 100
        rows.append(["Giá so với giá vốn LTH (holder ≥ 155 ngày)", light(d, good_above=10, bad_below=0),
                     f"{d:+.1f}% (LTH {fmt_usd(lth)})",
                     "Giá tiến sát/thủng LTH thường chỉ xảy ra ở pha suy yếu sâu."])

    loss_now, loss_date = latest_value(onchain_df, "supply_in_loss_percent")
    loss_7 = value_days_before(onchain_df, "supply_in_loss_percent", loss_date, 7)
    if loss_now is not None and loss_7 is not None:
        d = loss_now - loss_7
        rows.append(["% cung đang lỗ — thay đổi 7 ngày", light(d, good_below=-1, bad_above=1),
                     f"{loss_now:.1f}% ({d:+.1f} điểm %)", "Giảm: ít người lỗ hơn → áp lực bán hòa vốn nhẹ đi."])

    if cur_frame is not None and frame_7d_ago is not None and price:
        old = btc_in_range(frame_7d_ago, price, max(ath, price))
        new = btc_in_range(cur_frame, price, max(ath, price))
        pct = (new - old) / old * 100 if old > 0 else np.nan
        rows.append(["Cung giá hiện tại → ATH (cấu trúc 7 ngày, cùng mốc giá)", light(pct, good_below=-1, bad_above=1),
                     f"{fmt_btc(new - old, signed=True)} ({pct:+.2f}%)",
                     "Giảm: BTC giá vốn cao đã đổi tay/xuống thấp hơn → cung chờ hòa vốn phía trên mỏng đi."])

    if cur_frame is not None and price:
        s = btc_in_range(cur_frame, price * (1 - NEAR_PCT), price)
        r = btc_in_range(cur_frame, price, price * (1 + NEAR_PCT))
        bal = (s - r) / (s + r) * 100 if (s + r) > 0 else np.nan
        rows.append(["Cán cân hỗ trợ / cản trong ±10% quanh giá", light(bal, good_above=10, bad_below=-10),
                     f"{bal:+.1f}% (hỗ trợ {fmt_btc(s)} · cản {fmt_btc(r)})",
                     "Dương: phía dưới giá dày hơn phía trên → nền đỡ dày hơn lượng cung chờ bán."])

    if realized_df is not None and not realized_df.empty:
        last7 = realized_df.dropna(subset=["realized_profit", "realized_loss"]).tail(7)
        if len(last7) >= 3:
            net = float((last7.realized_profit - last7.realized_loss.abs()).sum())
            rows.append(["Net Realized P/L 7 ngày", "🟢" if net > 0 else "🔴",
                         fmt_big_usd(net, signed=True),
                         "Dương: thị trường chủ yếu chốt lời. Âm: đang cắt lỗ (cắt lỗ dồn dập kéo dài đôi khi trùng vùng đáy)."])

    if onchain_df is not None and "sopr" in onchain_df.columns:
        s7 = onchain_df[["date", "sopr"]].dropna().tail(7)
        lag = (utc_today() - s7.date.iloc[-1]).days if len(s7) else 999
        if len(s7) >= 3 and lag <= 12:   # nguồn miễn phí trễ ~7 ngày; cũ hơn 12 ngày thì bỏ
            v = float(s7.sopr.mean())
            rows.append(["SOPR trung bình 7 ngày", light(v, good_above=1.005, bad_below=0.995),
                         f"{v:.3f} (đến {s7.date.iloc[-1]:%d/%m})",
                         "> 1: coin được bán có lãi. < 1: bán lỗ. Nguồn miễn phí BGeometrics trễ ~7 ngày."])

    if etf and etf.get("sessions", 0) >= 5:
        v = etf["sum_5"]
        rows.append(["Dòng tiền ETF BTC 5 phiên", light(v / 1e6, good_above=250, bad_below=-250),
                     f"{fmt_big_usd(v, signed=True)} (phiên {etf['date']:%d/%m}: {fmt_big_usd(etf['last'], signed=True)})",
                     "Hút ròng: tổ chức/quỹ đang mua BTC thật qua ETF. Rút ròng kéo dài thường đi cùng pha giá yếu."])

    greens = sum(1 for r in rows if r[1] == "🟢")
    reds = sum(1 for r in rows if r[1] == "🔴")
    if not rows:
        verdict = "⚪ Chưa đủ dữ liệu"
    elif greens - reds >= 2:
        verdict = "🟢 Nghiêng tích cực"
    elif reds - greens >= 2:
        verdict = "🔴 Nghiêng tiêu cực"
    else:
        verdict = "🟡 Trung tính / trái chiều"
    return rows, greens, reds, verdict


def scenario_text(price, supports, resistances):
    """Kịch bản cần theo dõi dựa trên vùng hỗ trợ/kháng cự gần nhất (không phải khuyến nghị)."""
    lines = []
    if supports:
        s1 = supports[0]
        nxt = f" → vùng kế tiếp {fmt_usd(supports[1]['low'])}–{fmt_usd(supports[1]['high'])}" if len(supports) > 1 else ""
        lines.append(f"Hỗ trợ gần nhất **{fmt_usd(s1['low'])}–{fmt_usd(s1['high'])}** ({fmt_btc(s1['btc'])} giá vốn, "
                     f"cách {s1['distance_pct']:+.1f}%). Mất vùng này{nxt}.")
    if resistances:
        r1 = resistances[0]
        nxt = f" → vùng kế tiếp {fmt_usd(resistances[1]['low'])}–{fmt_usd(resistances[1]['high'])}" if len(resistances) > 1 else ""
        lines.append(f"Kháng cự gần nhất **{fmt_usd(r1['low'])}–{fmt_usd(r1['high'])}** ({fmt_btc(r1['btc'])} chờ hòa vốn, "
                     f"cách {r1['distance_pct']:+.1f}%). Vượt và giữ trên vùng này{nxt}.")
    return lines


def daily_brief(price, price_chg_24h, onchain_df, realized_df, cur_date, cur_frame, prev_frame,
                supports, resistances, events, etf=None, etf_alts=None):
    """Khối 'Hôm nay có gì thay đổi' — danh sách câu ngắn (markdown)."""
    out = []
    if price:
        chg = f" ({price_chg_24h:+.2f}% 24h)" if price_chg_24h is not None else ""
        out.append(f"💲 Giá BTC **{fmt_usd(price)}**{chg}.")
    loss_now, loss_date = latest_value(onchain_df, "supply_in_loss_percent")
    if loss_now is not None:
        l1 = value_days_before(onchain_df, "supply_in_loss_percent", loss_date, 1)
        l7 = value_days_before(onchain_df, "supply_in_loss_percent", loss_date, 7)
        parts = []
        if l1 is not None:
            parts.append(f"{loss_now - l1:+.1f} điểm % so với hôm trước")
        if l7 is not None:
            parts.append(f"{loss_now - l7:+.1f} so với 7 ngày trước")
        out.append(f"📉 Cung đang lỗ **{loss_now:.1f}%** ngày {day_str(loss_date)}"
                   + (f" ({', '.join(parts)})." if parts else "."))
    if cur_frame is not None and prev_frame is not None:
        bd = bucket_deltas(cur_frame, prev_frame)
        if not bd.empty:
            up = bd.loc[bd.delta.idxmax()]
            dn = bd.loc[bd.delta.idxmin()]
            out.append(f"🧱 URPD {cur_date}: vùng {fmt_usd(up.low)}–{fmt_usd(up.high)} tăng mạnh nhất "
                       f"({fmt_btc(up.delta, signed=True)}); vùng {fmt_usd(dn.low)}–{fmt_usd(dn.high)} giảm mạnh nhất "
                       f"({fmt_btc(dn.delta, signed=True)}).")
    if realized_df is not None and not realized_df.empty:
        r = realized_df.dropna(subset=["realized_profit", "realized_loss"])
        if not r.empty:
            last = r.iloc[-1]
            net = last.realized_profit - abs(last.realized_loss)
            out.append(f"💰 Realized {day_str(last.date)}: chốt lời {fmt_big_usd(last.realized_profit)}, "
                       f"cắt lỗ {fmt_big_usd(abs(last.realized_loss))} → ròng **{fmt_big_usd(net, signed=True)}**.")
    if etf:
        out.append(etf_brief_line(etf))
    if etf_alts:
        out.append(etf_alts_line(etf_alts) or "")
        out = [x for x in out if x]
    if supports or resistances:
        s = f"{fmt_usd(supports[0]['low'])}–{fmt_usd(supports[0]['high'])}" if supports else "N/A"
        r = f"{fmt_usd(resistances[0]['low'])}–{fmt_usd(resistances[0]['high'])}" if resistances else "N/A"
        out.append(f"🎯 Hỗ trợ gần nhất **{s}** · kháng cự gần nhất **{r}** (cụm giá vốn URPD dày nhất).")
    big = [e for e in (events or []) if "Rất cao" in e.get("impact", "")]
    if big:
        e = big[0]
        out.append(f"⏰ Sự kiện lớn sắp tới: **{e['event']}** — {e['date']} {e['time']}.")
    return out


def build_heatmap_data(frames, prices, days, bin_size=1000.0, max_cap=250000.0):
    """Ma trận lượng BTC theo vùng giá × ngày từ history (không gọi API)."""
    dates = sorted(frames)
    if len(dates) < 2:
        return None
    cutoff = parse_day(dates[-1]) - timedelta(days=int(days))
    sel = [d for d in dates if parse_day(d) >= cutoff]
    if len(sel) < 2:
        return None
    max_price = min(max(float(frames[d].price_high.max()) for d in sel), max_cap)
    dist, edges = [], None
    for d in sel:
        edges, amt = rebin_fixed(frames[d], bin_size, max_price=max_price)
        dist.append(amt)
    z = np.vstack(dist).T
    delta = np.diff(np.hstack([z[:, :1], z]), axis=1)
    return {"dates": sel, "edges": edges, "z": z, "delta": delta,
            "prices": [prices.get(d, np.nan) for d in sel]}


# =============================================================================
# CHẾ ĐỘ CẬP NHẬT DỮ LIỆU — chạy bởi GitHub Actions:  python app.py --update-data
# =============================================================================
def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_json(path, data, compact=False):
    with open(path, "w", encoding="utf-8") as f:
        if compact:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(data, f, ensure_ascii=False, indent=1)


def _log(msg):
    print(f"[{utc_now():%H:%M:%S}] {redact(msg)}", flush=True)


def clean_secret(raw):
    """Lấy đúng giá trị token kể cả khi lỡ dán cả dòng 'TÊN = "giá trị"' hoặc nhiều dòng."""
    text = str(raw or "").strip()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^[A-Za-z_][A-Za-z0-9_]*\s*=\s*(.+)$', line)
        if m:
            line = m.group(1).strip()
        return line.strip().strip('"').strip("'").strip()
    return ""


_SECRET_PATTERNS = [
    re.compile(r"github_pat_[A-Za-z0-9_]+"), re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),          # token bot Telegram
    re.compile(r"(?i)(header value:).*", re.S),                  # requests in lại nguyên header → cắt bỏ
    re.compile(r"(?i)(token|secret|password|api_key)\s*=\s*\S+"),
]


def redact(msg):
    """Che token/secret trước khi in log hoặc ghi vào update_status.json (repo có thể public)."""
    text = str(msg)
    for name in ("RESEARCHBITCOIN_API_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GITHUB_TOKEN"):
        raw = os.environ.get(name, "")
        for v in {raw.strip(), clean_secret(raw)}:
            if v and len(v) >= 6:
                text = text.replace(v, "***")
    for pat in _SECRET_PATTERNS:
        text = pat.sub(lambda m: (m.group(1) + " ***") if m.groups() else "***", text)
    return text


def _daily_dates(daily, key):
    return sorted(d for d, rec in daily.items() if is_day_key(d) and isinstance(rec, dict) and rec.get(key) is not None)


def update_urpd(status):
    hist = _load_json(URPD_FILE)
    today = utc_today()
    saved = sorted(k for k, v in hist.items() if is_day_key(k) and isinstance(v, dict) and v.get("urpd"))
    if saved:
        last = parse_day(saved[-1])
        # Luôn lấy lại 2 ngày gần nhất + hôm nay (snapshot lấy giữa ngày chưa chốt sẽ được thay bằng bản cuối ngày);
        # đồng thời bù các ngày còn thiếu, tối đa 30 ngày.
        start = min(last + timedelta(days=1), today - timedelta(days=2))
        start = max(start, today - timedelta(days=30))
    else:
        start = today - timedelta(days=URPD_INITIAL_DAYS - 1)

    changed, errors = [], []
    d = start
    while d <= today:
        key = day_str(d)
        try:
            frame, url, close, total_supply = fetch_bitview_urpd(key)
            price = safe_float(close)
            if price is None:
                raise ValueError("Bitview không trả giá close")
            snap = {
                "date": key, "price": price, "total_urpd": float(frame.btc_amount.sum()),
                "urpd": urpd_to_records(frame), "source": "Bitview /api/urpd/all (lin1000)", "url": url,
                "bitview_total_supply": safe_float(total_supply),
                "fetched_at_utc": utc_now().strftime("%Y-%m-%d %H:%M"),
            }
            if snapshot_changed(hist.get(key), snap):
                hist[key] = snap
                changed.append(key)
        except Exception as e:
            errors.append(f"{key}: {e}")
        d += timedelta(days=1)
        time.sleep(0.3)

    # Bù dần lịch sử cũ cho đủ HISTORY_MAX_DAYS ngày (tối đa URPD_BACKFILL_PER_RUN ngày mỗi lần chạy).
    oldest_wanted = today - timedelta(days=HISTORY_MAX_DAYS - 1)
    missing = []
    d = start - timedelta(days=1)
    while d >= oldest_wanted and len(missing) < URPD_BACKFILL_PER_RUN:
        if day_str(d) not in hist:
            missing.append(d)
        d -= timedelta(days=1)
    for d in missing:
        key = day_str(d)
        try:
            frame, url, close, total_supply = fetch_bitview_urpd(key)
            price = safe_float(close)
            if price is None:
                raise ValueError("Bitview không trả giá close")
            hist[key] = {"date": key, "price": price, "total_urpd": float(frame.btc_amount.sum()),
                         "urpd": urpd_to_records(frame), "source": "Bitview /api/urpd/all (lin1000)", "url": url,
                         "bitview_total_supply": safe_float(total_supply)}
            changed.append(key)
        except Exception as e:
            errors.append(f"bù {key}: {str(e)[:150]}")
        time.sleep(0.3)

    # URPD riêng của holder dài hạn (LTH ≥ 155 ngày) — để biết BTC rời vùng giá là của holder lâu năm hay mới.
    # Lấy cho các ngày vừa tải lại + bù các ngày gần đây còn thiếu (tối đa 35 ngày).
    lth_done = 0
    for key in sorted(k for k in hist if is_day_key(k) and parse_day(k) >= today - timedelta(days=35)):
        snap = hist[key]
        if not isinstance(snap, dict) or not snap.get("urpd"):
            continue
        if snap.get("urpd_lth") and parse_day(key) < start:
            continue
        try:
            lf, _, _, lsup = fetch_bitview_urpd(key, cohort="lth")
            recs = urpd_to_records(lf)
            if recs != snap.get("urpd_lth"):
                snap["urpd_lth"] = recs
                snap["lth_supply"] = safe_float(lsup) or float(lf.btc_amount.sum())
                if key not in changed:
                    changed.append(key)
                lth_done += 1
        except Exception as e:
            errors.append(f"LTH {key}: {str(e)[:150]}")
        time.sleep(0.3)

    if not saved and not changed:  # chưa có gì: thử BGeometrics cho hôm qua
        key = day_str(today - timedelta(days=1))
        try:
            frame, url = fetch_bgeometrics_urpd(key)
            hist[key] = {"date": key, "price": fetch_live_price(), "total_urpd": float(frame.btc_amount.sum()),
                         "urpd": urpd_to_records(frame), "source": "BGeometrics /v1/urpd (dự phòng)", "url": url}
            changed.append(key)
        except Exception as e:
            errors.append(f"BGeometrics dự phòng: {e}")

    if changed:
        keep = sorted(k for k in hist if is_day_key(k))[-HISTORY_MAX_DAYS:]
        hist = {k: hist[k] for k in keep}
        _save_json(URPD_FILE, hist, compact=True)
    latest = max((k for k in hist if is_day_key(k)), default=None)
    status["urpd"] = {"latest_date": latest, "changed": changed, "errors": errors[-5:]}
    _log(f"URPD: cập nhật {changed or 'không có gì'}; lỗi {len(errors)}")


def update_research(status):
    token = clean_secret(os.environ.get("RESEARCHBITCOIN_API_TOKEN", ""))
    if not token:
        status["research"] = {"note": "Không có RESEARCHBITCOIN_API_TOKEN — bỏ qua."}
        _log("ResearchBitcoin: không có token, bỏ qua")
        return
    data = _load_json(RESEARCH_FILE)
    data.setdefault("version", 3)
    daily = data.setdefault("daily", {})
    today = utc_today()
    yesterday = today - timedelta(days=1)
    backfill = max(1, int(os.environ.get("RB_BACKFILL_DAYS", "90")))
    updated, errors = {}, []
    for slug, key in RESEARCH_METRICS.items():
        have = _daily_dates(daily, key)
        if have and have[-1] >= day_str(yesterday):
            continue
        start = parse_day(have[-1]) + timedelta(days=1) if have else yesterday - timedelta(days=backfill - 1)
        need = (yesterday - start).days + 1
        quota = researchbitcoin_quota(token)
        if quota is not None:
            if quota < 1:
                errors.append(f"Hết quota ({quota:.0f} DP) — dừng.")
                break
            if quota < need:  # quota không đủ: chỉ lấy những ngày gần nhất
                start = yesterday - timedelta(days=int(quota) - 1)
        try:
            frame = researchbitcoin_fetch_series(token, slug, start, today)
            n = 0
            for row in frame.itertuples(index=False):
                if row.date <= yesterday:
                    daily.setdefault(day_str(row.date), {})[key] = float(row.value)
                    n += 1
            updated[key] = n
        except Exception as e:
            errors.append(redact(e)[:300])
            _log(f"  ⚠️ {slug}: {redact(e)[:300]}")
    if updated:
        data["daily"] = dict(sorted((k, v) for k, v in daily.items() if is_day_key(k))[-DAILY_MAX_DAYS:])
        data["updated_at_utc"] = utc_now().strftime("%Y-%m-%d %H:%M")
        _save_json(RESEARCH_FILE, data)
    status["research"] = {"updated": updated, "errors": errors[-4:]}
    _log(f"ResearchBitcoin: {updated or 'không có gì mới'}; lỗi {errors[-2:] if errors else 0}")


BG_FREE_DELAY_DAYS = 8   # gói miễn phí BGeometrics: MVRV/NUPL/SOPR chỉ trả dữ liệu cũ hơn ~7 ngày


def _update_bg_file(path, slug_to_key, status_key, status, only_if_missing=None, delay_days=3):
    """Cập nhật file daily từ BGeometrics; mỗi chỉ số 1 request, chỉ gọi khi còn thiếu ngày."""
    data = _load_json(path)
    data.setdefault("version", 1)
    daily = data.setdefault("daily", {})
    yesterday = utc_today() - timedelta(days=1)
    updated, errors, pending = {}, [], []
    attempts = data.setdefault("last_attempt", {})
    attempted_now = False
    today_key = day_str(utc_today())
    for slug, key in slug_to_key.items():
        if only_if_missing and not only_if_missing(key):
            continue
        have = _daily_dates(daily, key)
        if have and have[-1] >= day_str(yesterday):
            continue
        # Nguồn thường công bố trễ 1–2 ngày: nếu đã thử hôm nay và dữ liệu chỉ trễ ≤ 3 ngày
        # thì không gọi lại (tiết kiệm lượt gọi gói miễn phí).
        if have and attempts.get(key) == today_key and have[-1] >= day_str(yesterday - timedelta(days=delay_days)):
            pending.append(key)
            continue
        start = parse_day(have[-1]) + timedelta(days=1) if have else yesterday - timedelta(days=BG_INITIAL_DAYS - 1)
        attempts[key] = today_key
        attempted_now = True
        try:
            frame = bgeometrics_fetch_daily(slug, start, yesterday)
            for row in frame.itertuples(index=False):
                daily.setdefault(day_str(row.date), {})[key] = float(row.value)
            updated[key] = len(frame)
        except Exception as e:
            msg = redact(e)[:200]
            if "Không đọc được time-series" in msg:
                pending.append(key)  # nguồn chưa công bố ngày mới — không phải lỗi
            else:
                errors.append(f"{slug}: {msg}")
                _log(f"  ⚠️ {slug}: {msg}")
        time.sleep(2)
    if pending:
        _log(f"  ⏳ Nguồn chưa có số liệu ngày mới cho: {', '.join(pending)} (sẽ thử lại ngày mai)")
    if updated or attempted_now:
        data["daily"] = dict(sorted((k, v) for k, v in daily.items() if is_day_key(k))[-DAILY_MAX_DAYS:])
        if updated:
            data["updated_at_utc"] = utc_now().strftime("%Y-%m-%d %H:%M")
        _save_json(path, data)
    status[status_key] = {"updated": updated, "errors": errors[-4:], "pending": pending}
    _log(f"{path}: {updated or 'không có gì mới'}; lỗi {len(errors)}")


def update_realized(status):
    _update_bg_file(REALIZED_FILE, {"realized-profit": "realized_profit", "realized-loss": "realized_loss",
                                    "btc-price": "btc_price"}, "realized", status)


def update_onchain(status):
    research_daily = _load_json(RESEARCH_FILE).get("daily", {})

    def rb_missing(key):
        # Giá vốn STH/LTH: chỉ lấy từ BGeometrics nếu ResearchBitcoin chưa có
        if key in ("sth_realized_price", "lth_realized_price"):
            have = _daily_dates(research_daily, key)
            return not have or have[-1] < day_str(utc_today() - timedelta(days=3))
        return True

    _update_bg_file(ONCHAIN_FILE, {"mvrv": "mvrv", "nupl": "nupl", "sopr": "sopr",
                                   "sth-realized-price": "sth_realized_price",
                                   "lth-realized-price": "lth_realized_price"},
                    "onchain", status, only_if_missing=rb_missing, delay_days=BG_FREE_DELAY_DAYS)
    try:
        info = estimate_mvrv_from_urpd()
        if info:
            status.setdefault("onchain", {})["mvrv_estimate"] = info
            _log(f"MVRV/NUPL ước tính từ URPD: {info}")
    except Exception as e:
        _log(f"MVRV ước tính lỗi: {e}")


def urpd_realized_price(records):
    """Giá vốn trung bình toàn thị trường (realized price) ≈ Σ(giữa bucket × BTC) / Σ BTC."""
    tot = sum(r["btc_amount"] for r in records)
    rc = sum((r["price_low"] + r["price_high"]) / 2 * r["btc_amount"] for r in records)
    return rc / tot if tot > 0 else None


def estimate_mvrv_from_urpd():
    """BGeometrics miễn phí trễ ~7 ngày → tự tính MVRV/NUPL các ngày còn thiếu từ URPD (hiệu chỉnh theo BGeometrics)."""
    hist = _load_json(URPD_FILE)
    data = _load_json(ONCHAIN_FILE)
    daily = data.setdefault("daily", {})
    raw = {}
    for d, v in hist.items():
        if is_day_key(d) and isinstance(v, dict) and v.get("urpd") and safe_float(v.get("price")):
            rp = urpd_realized_price(v["urpd"])
            if rp:
                raw[d] = float(v["price"]) / rp
    if not raw:
        return None
    pairs = [(d, daily[d]["mvrv"] / raw[d]) for d in sorted(raw)
             if isinstance(daily.get(d), dict) and safe_float(daily[d].get("mvrv")) and raw[d] > 0]
    k = float(np.median([x for _, x in pairs[-30:]])) if len(pairs) >= 5 else 1.0
    bg_last = max((d for d, r in daily.items() if isinstance(r, dict) and safe_float(r.get("mvrv"))), default="")
    n = 0
    for d in sorted(raw):
        rec = daily.setdefault(d, {})
        if d > bg_last:
            m = raw[d] * k
            rec["mvrv_est"], rec["nupl_est"] = round(m, 4), round(1 - 1 / m, 4)
            n += 1
        else:  # BGeometrics đã có số thật → bỏ ước tính cũ
            rec.pop("mvrv_est", None)
            rec.pop("nupl_est", None)
    data["daily"] = dict(sorted((k_, v) for k_, v in daily.items() if is_day_key(k_))[-DAILY_MAX_DAYS:])
    data["mvrv_estimate"] = {"k": round(k, 4), "calib_days": min(len(pairs), 30), "bg_last": bg_last, "est_days": n}
    _save_json(ONCHAIN_FILE, data)
    return data["mvrv_estimate"]


def _daily_file_frame(path):
    daily = _load_json(path).get("daily", {})
    rows = [{"date": parse_day(d), **rec} for d, rec in daily.items() if is_day_key(d) and isinstance(rec, dict)]
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True) if rows else pd.DataFrame(columns=["date"])


def _fix_loss_percent(df):
    """ResearchBitcoin trả supply_in_loss_percent dạng tỷ lệ 0–1 (vd 0.37 = 37%) → đổi sang %."""
    if df is not None and "supply_in_loss_percent" in df.columns:
        df = df.copy()
        v = pd.to_numeric(df["supply_in_loss_percent"], errors="coerce")
        df["supply_in_loss_percent"] = np.where(v <= 1.5, v * 100.0, v)
    return df


def _apply_estimates(df):
    """MVRV/NUPL: số thật BGeometrics nếu có, các ngày gần đây (nguồn miễn phí trễ) dùng ước tính từ URPD."""
    for base in ("mvrv", "nupl"):
        est = f"{base}_est"
        if est in df.columns:
            if base in df.columns:
                df[f"{base}_is_est"] = df[base].isna() & df[est].notna()
                df[base] = df[base].combine_first(df[est])
            else:
                df[f"{base}_is_est"] = df[est].notna()
                df[base] = df[est]
            df = df.drop(columns=[est])
    return df


def merge_onchain_frames(research_df, bg_df):
    """Gộp ResearchBitcoin (ưu tiên) + BGeometrics theo ngày."""
    if bg_df is not None and not bg_df.empty:
        bg_df = _apply_estimates(bg_df.copy())
    frames = [f for f in (research_df, bg_df) if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame(columns=["date"])
    if len(frames) == 1:
        return _fix_loss_percent(frames[0])
    m = research_df.merge(bg_df, on="date", how="outer", suffixes=("", "_bg")).sort_values("date")
    for col in [c for c in m.columns if c.endswith("_bg")]:
        base = col[:-3]
        m[base] = m[base].combine_first(m[col]) if base in m.columns else m[col]
        m = m.drop(columns=[col])
    return _fix_loss_percent(m.reset_index(drop=True))


def _telegram_html(md):
    text = md.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)


def send_telegram_report(status):
    bot = clean_secret(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    chat = clean_secret(os.environ.get("TELEGRAM_CHAT_ID", ""))
    if not bot or not chat:
        return
    try:
        hist = _load_json(URPD_FILE)
        dates = sorted(k for k, v in hist.items() if is_day_key(k) and isinstance(v, dict) and v.get("urpd"))
        if not dates:
            return
        cur_date = dates[-1]
        cur = records_to_urpd(hist[cur_date]["urpd"])
        prev_key = day_str(parse_day(cur_date) - timedelta(days=1))
        prev = records_to_urpd(hist[prev_key]["urpd"]) if prev_key in hist and hist[prev_key].get("urpd") else None
        price = fetch_live_price() or safe_float(hist[cur_date].get("price"))
        onchain = merge_onchain_frames(_daily_file_frame(RESEARCH_FILE), _daily_file_frame(ONCHAIN_FILE))
        realized = _daily_file_frame(REALIZED_FILE)
        supports, resistances = find_sr_zones(cur, price)
        k7 = day_str(parse_day(cur_date) - timedelta(days=7))
        f7 = records_to_urpd(hist[k7]["urpd"]) if k7 in hist and hist[k7].get("urpd") else None
        etf_data = _load_json(ETF_FILE)
        etf = etf_stats(etf_frame(etf_data))
        sig_rows, greens, reds, verdict = build_signals(price, onchain, realized, cur, f7, DEFAULT_ATH, etf)
        mdf = market_frame(_load_json(MARKET_FILE))
        comp = market_composite(sig_rows, mdf, price)

        tg = status.setdefault("telegram", {})
        alerts = []
        sth, _ = latest_value(onchain, "sth_realized_price")
        if sth and price:
            state = "above" if price >= sth else "below"
            if tg.get("sth_state") and tg["sth_state"] != state:
                alerts.append(f"🚨 Giá vừa {'VƯỢT LÊN' if state == 'above' else 'THỦNG'} giá vốn STH {fmt_usd(sth)}.")
            tg["sth_state"] = state
        loss_now, _ = latest_value(onchain, "supply_in_loss_percent")
        threshold = float(os.environ.get("TELEGRAM_LOSS_ALERT_PCT", "50"))
        if loss_now is not None:
            state = "above" if loss_now >= threshold else "below"
            if tg.get("loss_state") and tg["loss_state"] != state:
                alerts.append(f"🚨 % cung đang lỗ {'vượt lên trên' if state == 'above' else 'rơi xuống dưới'} "
                              f"{threshold:.0f}% (hiện {loss_now:.1f}%).")
            tg["loss_state"] = state

        today_key = day_str(utc_today())
        send_daily = tg.get("last_daily") != today_key
        if not send_daily and not alerts:
            return
        lines = [f"<b>BTC URPD Monitor — {today_key}</b>"]
        lines += [_telegram_html(a) for a in alerts]
        if send_daily:
            brief = daily_brief(price, None, onchain, realized, cur_date, cur, prev, supports, resistances,
                                upcoming_btc_events(7, rows=fetch_economic_calendar_raw()), etf, etf_alt_frames(etf_data))
            brief += market_extra_brief(mdf)
            lines += ["• " + _telegram_html(b) for b in brief]
            lines.append(_telegram_html(f"Tín hiệu on-chain: **{verdict}** ({greens} 🟢 / {reds} 🔴)"))
            if comp:
                head, cond, agree = composite_outlook(comp, supports, resistances)
                lines.append(_telegram_html(f"🧭 Điểm thị trường tổng hợp: **{comp['score']:.0f}/100 — {comp['verdict']}** · {agree}"))
                lines.append(_telegram_html(head + (" " + cond if cond else "")))
        r = requests.post(f"https://api.telegram.org/bot{bot}/sendMessage",
                          json={"chat_id": chat, "text": "\n".join(lines), "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=20)
        r.raise_for_status()
        if send_daily:
            tg["last_daily"] = today_key
        _log("Telegram: đã gửi")
    except Exception as e:
        status.setdefault("telegram", {})["error"] = redact(e)[:300]
        _log(f"Telegram lỗi: {e}")


def update_market(status):
    try:
        m = fetch_market_overview()
        m["fetched_at_utc"] = utc_now().strftime("%Y-%m-%d %H:%M")
        status["market"] = m
        _log("Vốn hóa thị trường: OK")
    except Exception as e:
        _log(f"Vốn hóa thị trường lỗi: {e}")


# =============================================================================
# DÒNG TIỀN ETF BTC GIAO NGAY (Mỹ)
# =============================================================================
ETF_FILE = "etf_flows_history.json"
ETF_ORDER = ["IBIT", "FBTC", "GBTC", "BTC", "ARKB", "BITB", "HODL", "BRRR", "EZBC", "BTCO", "BTCW", "DEFI", "MSBT"]
ETF_NAMES = {"IBIT": "BlackRock", "FBTC": "Fidelity", "GBTC": "Grayscale", "BTC": "Grayscale Mini", "ARKB": "Ark 21Shares",
             "BITB": "Bitwise", "HODL": "VanEck", "BRRR": "CoinShares/Valkyrie", "EZBC": "Franklin", "BTCO": "Invesco",
             "BTCW": "WisdomTree", "DEFI": "Hashdex", "MSBT": "Morgan Stanley"}
ETF_REFRESH_DAYS = 10           # mỗi lần chạy ghi đè 10 phiên gần nhất (quỹ báo số muộn T+1)
ETF_MIN_INTERVAL_H = 6          # SoSoValue gói Demo 10.000 lượt/tháng → chỉ gọi lại sau 6 giờ (dữ liệu ETF đổi 1 lần/ngày)
# ETF altcoin giao ngay tại Mỹ (SoSoValue). Mã quỹ biết trước; app tự dò thêm mã mới cho NEAR/LINK.
ETF_ALTS = {"NEAR": ["NRR", "GSNR"], "LINK": ["GLNK", "CLNK"], "ETH": [], "SOL": [], "XRP": []}
ETF_ALT_FUNDS_FOR = ("NEAR", "LINK")   # chỉ lấy số từng quỹ cho các coin ưu tiên (tiết kiệm lượt gọi)
ETF_ALT_LABEL = {"NEAR": "Ⓝ NEAR", "LINK": "🔗 Chainlink", "ETH": "Ξ Ethereum", "SOL": "◎ Solana", "XRP": "✕ XRP"}
ETF_NAMES.update({"NRR": "Bitwise", "GSNR": "Grayscale", "GLNK": "Grayscale", "CLNK": "Bitwise"})
SOSO_BASE = "https://openapi.sosovalue.com/openapi/v1"
FARSIDE_URL = "https://farside.co.uk/bitcoin-etf-flow-all-data/"
YASIN_URL = "https://yasinozen.com/etf-flows/api/v1/flows"
BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                                 "Chrome/124.0 Safari/537.36",
                   "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                   "Accept-Language": "en-GB,en;q=0.9"}


def _etf_day(text):
    """'2026-11-04', '04 Nov 2026', '4 November 2026', epoch ms → 'YYYY-MM-DD'."""
    if text is None:
        return None
    if isinstance(text, (int, float)) and text > 1e11:
        return day_str(datetime.fromtimestamp(text / 1000, tz=timezone.utc).date())
    s = re.sub(r"\s+", " ", str(text)).strip()
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%Y/%m/%d"):
        try:
            return day_str(datetime.strptime(s[:20].strip(), fmt).date())
        except ValueError:
            continue
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else None


def _etf_num(text):
    """'(123.4)' → -123.4 · '1,234.5' → 1234.5 · '-' / '' → None."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return safe_float(text)
    s = str(text).strip().replace(",", "").replace("$", "").replace("−", "-")
    if s in ("", "-", "–", "—", "n/a", "N/A", "null"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    v = safe_float(s.strip("()"))
    return -v if (v is not None and neg) else v


def _etf_scale(days):
    """Nguồn trả đơn vị triệu USD → đổi sang USD (dòng tiền 1 phiên luôn ≥ vài trăm nghìn USD)."""
    totals = [abs(r["total"]) for r in days.values() if r.get("total")]
    if totals and max(totals) < 1e5:
        for r in days.values():
            for k in ("total", "aum", "cum"):
                if r.get(k) is not None:
                    r[k] *= 1e6
            r["funds"] = {t: v * 1e6 for t, v in (r.get("funds") or {}).items()}
    return days


def _soso_rows(payload):
    if isinstance(payload, dict):
        if payload.get("code") not in (None, 0, "0", 200):
            raise RuntimeError(f"SoSoValue: {payload.get('msg') or payload.get('code')}")
        payload = payload.get("data", payload)
        if isinstance(payload, dict):
            payload = payload.get("list") or payload.get("items") or []
    return payload if isinstance(payload, list) else []


def soso_list_tickers(api_key, symbol):
    """Dò danh sách mã ETF của 1 coin (nếu API hỗ trợ) — lỗi thì trả rỗng."""
    try:
        r = requests.get(f"{SOSO_BASE}/etfs", headers={"x-soso-api-key": api_key, "Accept": "application/json"},
                         params={"symbol": symbol, "country_code": "US"}, timeout=20)
        if r.status_code != 200:
            return []
        out = []
        for row in _soso_rows(r.json()):
            t = str((row or {}).get("ticker") or "").upper() if isinstance(row, dict) else ""
            if re.fullmatch(r"[A-Z]{2,6}", t) and t not in out:
                out.append(t)
        return out[:8]
    except Exception:
        return []


def fetch_etf_sosovalue(api_key, tickers=ETF_ORDER, symbol="BTC"):
    """SoSoValue OpenAPI (cần key miễn phí, header x-soso-api-key). Lịch sử tối đa ~1 tháng."""
    h = {"x-soso-api-key": api_key, "Accept": "application/json"}
    r = requests.get(f"{SOSO_BASE}/etfs/summary-history", headers=h,
                     params={"symbol": symbol, "country_code": "US", "limit": 300}, timeout=30)
    r.raise_for_status()
    days = {}
    for row in _soso_rows(r.json()):
        d = _etf_day(row.get("date"))
        tot = safe_float(row.get("total_net_inflow"))
        if d and tot is not None:
            days[d] = {"total": tot, "funds": {}, "aum": safe_float(row.get("total_net_assets")),
                       "cum": safe_float(row.get("cum_net_inflow")), "traded": safe_float(row.get("total_value_traded"))}
    for t in tickers:
        try:
            rr = requests.get(f"{SOSO_BASE}/etfs/{t}/history", headers=h, timeout=20)
            if rr.status_code != 200:
                continue
            for row in _soso_rows(rr.json()):
                d, v = _etf_day(row.get("date")), safe_float(row.get("net_inflow"))
                if d in days and v is not None:
                    days[d]["funds"][t] = v
            time.sleep(0.7)
        except Exception:
            continue
    return days


def parse_farside_html(html):
    """Bảng Farside: hàng tiêu đề có mã quỹ (IBIT, FBTC…, Total); mỗi hàng dữ liệu bắt đầu bằng ngày '04 Nov 2026';
    số âm viết '(12.3)', đơn vị US$ triệu."""
    days, header = {}, None
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S | re.I):
        cells = [re.sub(r"<[^>]+>", "", c).replace("&nbsp;", " ").strip()
                 for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, flags=re.S | re.I)]
        if not cells:
            continue
        up = [c.upper() for c in cells]
        if "IBIT" in up and "TOTAL" in up:
            header = up
            continue
        d = _etf_day(cells[0])
        if not header or not d or len(cells) != len(header):
            continue
        funds = {}
        for name, val in zip(header[1:], cells[1:]):
            v = _etf_num(val)
            if name != "TOTAL" and re.fullmatch(r"[A-Z]{2,5}", name) and v is not None:
                funds[name] = v
        if "TOTAL" in header:
            t = _etf_num(cells[header.index("TOTAL")])
            if t is None and funds:
                t = sum(funds.values())
            if t is not None:
                days[d] = {"total": t * 1e6, "funds": {k: v * 1e6 for k, v in funds.items()}}
    return days


def fetch_etf_farside():
    r = requests.get(FARSIDE_URL, headers=BROWSER_HEADERS, timeout=40)
    r.raise_for_status()
    days = parse_farside_html(r.text)
    if not days:
        raise RuntimeError("Farside: không đọc được bảng (có thể bị chặn bot)")
    return days


def fetch_etf_yasinozen(api_key=""):
    """API miễn phí dựng từ bảng Farside (không key: ~7 phiên tổng; key miễn phí: 90 phiên + từng quỹ)."""
    h = {"Accept": "application/json", "User-Agent": HTTP_HEADERS["User-Agent"]}
    if api_key:
        h["X-API-Key"] = api_key
    r = requests.get(YASIN_URL, headers=h, params={"asset": "BTC", "limit": 200, "include_funds": "true"}, timeout=30)
    r.raise_for_status()
    js = r.json()
    rows = js.get("data") if isinstance(js, dict) else js
    days = {}
    for row in rows or []:
        d = _etf_day(row.get("flow_date") or row.get("date"))
        if not d:
            continue
        rec = days.setdefault(d, {"total": None, "funds": {}})
        tk = str(row.get("ticker") or row.get("fund") or "").upper()
        v = _etf_num(row.get("net_flow_usd", row.get("net_flow")))
        if isinstance(row.get("funds"), dict):
            rec["total"] = v
            rec["funds"].update({k.upper(): _etf_num(x) for k, x in row["funds"].items() if _etf_num(x) is not None})
        elif isinstance(row.get("funds"), list):
            rec["total"] = v
            for f in row["funds"]:
                fv = _etf_num(f.get("net_flow_usd", f.get("net_flow")))
                if f.get("ticker") and fv is not None:
                    rec["funds"][str(f["ticker"]).upper()] = fv
        elif tk in ("", "TOTAL", "ALL"):
            rec["total"] = v
        elif v is not None:
            rec["funds"][tk] = v
    for rec in days.values():
        if rec["total"] is None and rec["funds"]:
            rec["total"] = sum(rec["funds"].values())
    return {d: r for d, r in days.items() if r["total"] is not None}


def update_etf(status):
    """Thử lần lượt: SoSoValue (nếu có SOSOVALUE_API_KEY) → Farside → yasinozen API. Nguồn nào được thì dùng."""
    st_etf = status.setdefault("etf", {"errors": []})
    data = _load_json(ETF_FILE)
    daily = data.get("daily", {}) if isinstance(data.get("daily"), dict) else {}
    sources = []
    soso = clean_secret(os.environ.get("SOSOVALUE_API_KEY", ""))
    try:
        age_h = (utc_now() - datetime.strptime(data["updated_at_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                 ).total_seconds() / 3600
    except Exception:
        age_h = 999
    have_alts = set((data.get("alts") or {}).keys()) >= set(ETF_ALTS) or not soso
    alt_missing = any((v or {}).get("error") for v in (data.get("alts") or {}).values() if isinstance(v, dict)) \
        and data.get("app_version") != APP_VERSION      # bản app mới (vd sửa lỗi NEAR) → lấy lại ngay
    expected = utc_today() - timedelta(days=1)          # phiên Mỹ gần nhất đáng lẽ đã có số
    while expected.weekday() >= 5:
        expected -= timedelta(days=1)
    behind = bool(daily) and max(daily) < day_str(expected)
    if daily and have_alts and not alt_missing and age_h < ETF_MIN_INTERVAL_H and os.environ.get("ETF_FORCE") != "1":
        skip = True
        if behind and soso:  # thiếu phiên mới → hỏi nhẹ 1 lượt xem SoSoValue đã có số chưa
            try:
                probe = _soso_rows(requests.get(f"{SOSO_BASE}/etfs/summary-history",
                                                headers={"x-soso-api-key": soso, "Accept": "application/json"},
                                                params={"symbol": "BTC", "country_code": "US", "limit": 3},
                                                timeout=20).json())
                newest = max((_etf_day(r.get("date")) or "" for r in probe), default="")
                skip = newest <= max(daily)
            except Exception:
                skip = True
        if skip:
            why = (f"SoSoValue chưa có phiên {expected:%d/%m}" if behind else f"đã lấy {age_h:.1f} giờ trước")
            st_etf.update({"source": data.get("source"), "latest": max(daily), "sessions": len(daily), "skipped": why})
            _log(f"ETF: bỏ qua ({why})")
            return
    if soso:
        sources.append(("SoSoValue", lambda: fetch_etf_sosovalue(soso)))
    sources.append(("Farside", fetch_etf_farside))
    sources.append(("yasinozen (Farside)",
                    lambda: _etf_scale(fetch_etf_yasinozen(clean_secret(os.environ.get("ETF_FLOWS_API_KEY", ""))))))
    got, used, notes = {}, None, []
    for name, fn in sources:
        try:
            got = fn()
            if got:
                used = name
                break
            notes.append(f"{name}: rỗng")
        except Exception as e:
            notes.append(f"{name}: {redact(e)[:140]}")
    if not got:
        st_etf["errors"] = notes or ["không có nguồn nào"]
        _log("ETF lỗi: " + " | ".join(notes))
        return
    if used != "Farside" and len(set(daily) | set(got)) < 60:
        try:  # nguồn chính chỉ có ~1 tháng → thử lấy thêm lịch sử cũ từ Farside (chỉ điền ngày còn thiếu)
            for d, rec in fetch_etf_farside().items():
                if d not in got and d not in daily:
                    daily[d] = rec
        except Exception:
            pass
    daily = _merge_etf_daily(daily, got)
    alts = data.get("alts") if isinstance(data.get("alts"), dict) else {}
    if soso:
        alts = update_etf_alts(soso, alts)
    _save_json(ETF_FILE, {"updated_at_utc": utc_now().strftime("%Y-%m-%d %H:%M"), "source": used, "daily": daily,
                          "alts": alts, "app_version": APP_VERSION})
    st_etf.update({"source": used, "latest": max(daily), "sessions": len(daily), "notes": notes,
                   "alts": {s: (v.get("error") or f"{len(v.get('daily', {}))} phiên") for s, v in alts.items()}})
    _log(f"ETF: {used} — {len(got)} phiên, mới nhất {max(got)}")


def _merge_etf_daily(daily, got):
    """Gộp dữ liệu mới vào lịch sử: 10 phiên gần nhất ghi đè (số quỹ báo muộn), phiên cũ chỉ bổ sung."""
    daily = dict(daily)
    keep_from = sorted(daily)[-ETF_REFRESH_DAYS] if len(daily) >= ETF_REFRESH_DAYS else ""
    for d, rec in got.items():
        old = daily.get(d)
        if old is None or d >= keep_from or (not old.get("funds") and rec.get("funds")):
            merged = dict(old or {})
            merged.update({k: v for k, v in rec.items() if v not in (None, {})})
            daily[d] = merged
    for rec in daily.values():  # làm tròn về USD
        for k in ("total", "aum", "cum", "traded"):
            if safe_float(rec.get(k)) is not None:
                rec[k] = round(float(rec[k]))
        if isinstance(rec.get("funds"), dict):
            rec["funds"] = {t: round(float(v)) for t, v in rec["funds"].items() if safe_float(v) is not None}
    cut = day_str(utc_today() - timedelta(days=DAILY_MAX_DAYS))
    return {d: daily[d] for d in sorted(daily) if d >= cut}


def fetch_etf_from_tickers(api_key, tickers):
    """Dự phòng khi SoSoValue không nhận mã coin (vd NEAR): cộng dữ liệu từng quỹ thành tổng."""
    h = {"x-soso-api-key": api_key, "Accept": "application/json"}
    days, errs = {}, []
    for t in tickers:
        try:
            r = requests.get(f"{SOSO_BASE}/etfs/{t}/history", headers=h, timeout=20)
            if r.status_code != 200:
                errs.append(f"{t}: HTTP {r.status_code}")
                continue
            for row in _soso_rows(r.json()):
                d, v = _etf_day(row.get("date")), safe_float(row.get("net_inflow"))
                if not d or v is None:
                    continue
                rec = days.setdefault(d, {"total": 0.0, "funds": {}, "aum": 0.0, "cum": 0.0})
                rec["total"] += v
                rec["funds"][t] = v
                rec["aum"] += safe_float(row.get("net_assets")) or 0.0
                rec["cum"] += safe_float(row.get("cum_inflow")) or 0.0
            time.sleep(0.7)
        except Exception as e:
            errs.append(f"{t}: {redact(e)[:80]}")
    for rec in days.values():
        rec["aum"] = rec["aum"] or None
        rec["cum"] = rec["cum"] or None
    if not days:
        raise RuntimeError("không lấy được theo từng quỹ (" + "; ".join(errs or ["không có dữ liệu"]) + ")")
    return days


def update_etf_alts(api_key, alts):
    """ETF altcoin (NEAR, LINK ưu tiên; ETH/SOL/XRP chỉ lấy tổng)."""
    alts = dict(alts)
    for sym, known in ETF_ALTS.items():
        cur = dict(alts.get(sym) or {})
        try:
            tickers = []
            if sym in ETF_ALT_FUNDS_FOR:
                tickers = list(dict.fromkeys(list(known) + soso_list_tickers(api_key, sym) + cur.get("tickers", [])))
            try:
                got = fetch_etf_sosovalue(api_key, tickers=tickers, symbol=sym)   # SoSoValue luôn trả USD
            except Exception as e:
                got, first_err = {}, e
            else:
                first_err = None
            if not got and (tickers or known):
                # mã coin không được hỗ trợ (vd NEAR trả 400) → cộng từ từng quỹ (NRR…)
                got = fetch_etf_from_tickers(api_key, tickers or list(known))
                _log(f"ETF {sym}: dùng dữ liệu từng quỹ {tickers or known}" + (f" (tổng lỗi: {redact(first_err)[:60]})" if first_err else ""))
            if not got:
                raise RuntimeError(redact(first_err)[:120] if first_err else "SoSoValue chưa có dữ liệu ETF cho coin này")
            seen = sorted({t for r in got.values() for t, v in (r.get("funds") or {}).items()})
            cur.update({"daily": _merge_etf_daily(cur.get("daily") or {}, got), "error": None,
                        "tickers": seen or cur.get("tickers", [])})
            _log(f"ETF {sym}: {len(got)} phiên, mới nhất {max(got)}")
        except Exception as e:
            cur["error"] = redact(e)[:140]
            _log(f"ETF {sym} lỗi: {cur['error']}")
        alts[sym] = cur
        time.sleep(0.7)
    return alts


def etf_frame(data):
    """dict file → DataFrame: date, total, aum, cum + 1 cột/quỹ (USD)."""
    daily = data.get("daily", {}) if isinstance(data, dict) else {}
    rows = []
    for d, rec in daily.items():
        if not is_day_key(d) or not isinstance(rec, dict) or safe_float(rec.get("total")) is None:
            continue
        row = {"date": parse_day(d), "total": float(rec["total"]),
               "aum": safe_float(rec.get("aum")), "cum": safe_float(rec.get("cum"))}
        for t, v in (rec.get("funds") or {}).items():
            row[f"f_{t}"] = safe_float(v)
        rows.append(row)
    if not rows:
        return pd.DataFrame(columns=["date", "total", "aum", "cum"])
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def etf_fund_cols(df):
    cols = [c for c in df.columns if c.startswith("f_")]
    order = {t: i for i, t in enumerate(ETF_ORDER)}
    return sorted(cols, key=lambda c: (order.get(c[2:], 99), c))


def etf_stats(df):
    """Số liệu tóm tắt cho thẻ số, tín hiệu, báo cáo."""
    if df is None or df.empty:
        return None
    s = df.total
    last = df.iloc[-1]
    sign = np.sign(last.total)
    streak = 0
    for v in reversed(s.tolist()):
        if np.sign(v) == sign and v != 0:
            streak += 1
        else:
            break
    out = {"date": last.date, "last": float(last.total), "sum_5": float(s.tail(5).sum()),
           "sum_20": float(s.tail(20).sum()), "streak": streak * int(sign or 1), "sessions": len(df),
           "aum": safe_float(last.get("aum")), "cum": safe_float(last.get("cum")), "funds_last": {}, "funds_5": {}}
    fc = etf_fund_cols(df)
    has = df[fc].notna().any(axis=1) if fc else pd.Series(False, index=df.index)
    flast = df[has].iloc[-1] if has.any() else last   # quỹ báo muộn T+1 → lấy phiên gần nhất đã đủ số từng quỹ
    out["funds_date"] = flast.date
    for c in fc:
        v = safe_float(flast.get(c))
        if v is not None:
            out["funds_last"][c[2:]] = v
        v5 = df[c].tail(5).sum(min_count=1)
        if safe_float(v5) is not None:
            out["funds_5"][c[2:]] = float(v5)
    return out


def etf_brief_line(stt):
    if not stt:
        return None
    streak = stt["streak"]
    sline = f"chuỗi {abs(streak)} phiên {'hút ròng' if streak > 0 else 'rút ròng'}" if abs(streak) >= 2 else ""
    top = ""
    if stt["funds_last"]:
        best = max(stt["funds_last"].items(), key=lambda kv: kv[1])
        worst = min(stt["funds_last"].items(), key=lambda kv: kv[1])
        parts = []
        if best[1] > 0:
            parts.append(f"{best[0]} {fmt_big_usd(best[1], signed=True)}")
        if worst[1] < 0:
            parts.append(f"{worst[0]} {fmt_big_usd(worst[1], signed=True)}")
        when = "" if stt.get("funds_date") == stt["date"] else f" phiên {stt['funds_date']:%d/%m}"
        top = f" ({', '.join(parts)}{when})" if parts else ""
    return (f"🏦 ETF BTC phiên {stt['date']:%d/%m}: **{fmt_big_usd(stt['last'], signed=True)}**{top}; "
            f"5 phiên {fmt_big_usd(stt['sum_5'], signed=True)}" + (f", {sline}" if sline else "") + ".")


# =============================================================================
# PHÁI SINH · STABLECOIN · COINBASE PREMIUM · SỢ HÃI & THAM LAM (nguồn miễn phí, không cần key)
# =============================================================================
MARKET_FILE = "market_history.json"
OKX_API = "https://www.okx.com/api/v5"


def _jget(url, params=None, timeout=25, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=timeout)
            if r.status_code == 429 and attempt < retries:
                time.sleep(10 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(2)
    raise RuntimeError(redact(last))


def _ts_day(ts):
    """ms/giây (số hoặc chuỗi) → 'YYYY-MM-DD' (UTC)."""
    v = safe_float(ts)
    if v is None:
        return None
    if v > 1e11:
        v /= 1000
    return day_str(datetime.fromtimestamp(v, tz=timezone.utc).date())


def fetch_btc_derivatives():
    """OKX: OI + khối lượng hợp đồng, long/short tài khoản, taker mua/bán, funding; Hyperliquid: ảnh chụp hiện tại."""
    out, now, errors = {}, {}, []
    try:
        for row in _jget(f"{OKX_API}/rubik/stat/contracts/open-interest-volume", {"ccy": "BTC", "period": "1D"}).get("data") or []:
            d = _ts_day(row[0])
            if d:
                out.setdefault(d, {}).update({"oi_usd": safe_float(row[1]), "perp_volume_usd": safe_float(row[2])})
    except Exception as e:
        errors.append(f"OKX OI: {redact(e)[:120]}")
    try:
        for row in _jget(f"{OKX_API}/rubik/stat/contracts/long-short-account-ratio", {"ccy": "BTC", "period": "1D"}).get("data") or []:
            d, v = _ts_day(row[0]), safe_float(row[1])
            if d and v is not None:
                out.setdefault(d, {})["long_short_ratio"] = v
    except Exception as e:
        errors.append(f"OKX long/short: {redact(e)[:120]}")
    taker_err = []
    for path, prm in (("taker-volume-contract", {"instId": "BTC-USDT-SWAP", "period": "1D", "unit": "1"}),
                      ("taker-volume", {"ccy": "BTC", "instType": "CONTRACTS", "period": "1D"})):
        try:  # [ts, sellVol, buyVol] — bản -contract cập nhật mỗi ngày; bản theo ccy hay chậm vài tuần
            rows = _jget(f"{OKX_API}/rubik/stat/{path}", prm).get("data") or []
            n = 0
            for row in rows:
                d, sell, buy = _ts_day(row[0]), safe_float(row[1]), safe_float(row[2])
                if d and sell is not None and buy is not None:
                    out.setdefault(d, {}).update({"taker_sell": sell, "taker_buy": buy})
                    n += 1
            if n:
                taker_err = []
                break
        except Exception as e:
            taker_err.append(f"OKX taker ({path}): {redact(e)[:100]}")
    errors += taker_err
    try:
        rows = [(int(r["fundingTime"]), safe_float(r.get("realizedRate") or r.get("fundingRate")))
                for r in _jget(f"{OKX_API}/public/funding-rate-history",
                               {"instId": "BTC-USDT-SWAP", "limit": 100}).get("data") or [] if r.get("fundingTime")]
        rows = sorted((t, v) for t, v in rows if v is not None)
        if len(rows) >= 2:
            interval_h = float(np.median(np.diff([t for t, _ in rows]) / 3.6e6)) or 8.0
            per_year = 24 * 365 / interval_h
            daily = {}
            for t, v in rows:
                daily.setdefault(_ts_day(t), []).append(v)
            for d, vs in daily.items():
                out.setdefault(d, {})["funding_apr"] = float(np.mean(vs)) * per_year * 100
    except Exception as e:
        errors.append(f"OKX funding: {redact(e)[:120]}")
    try:  # giá đóng cửa ngày (để quy OI ra BTC)
        for row in _jget(f"{OKX_API}/market/history-candles", {"instId": "BTC-USDT", "bar": "1Dutc", "limit": 100}).get("data") or []:
            d, c = _ts_day(row[0]), safe_float(row[4])
            if d and c:
                out.setdefault(d, {})["price"] = c
    except Exception as e:
        errors.append(f"OKX giá: {redact(e)[:120]}")
    try:
        r = requests.post("https://api.hyperliquid.xyz/info", json={"type": "metaAndAssetCtxs"}, timeout=20)
        r.raise_for_status()
        meta, ctxs = r.json()
        names = [u.get("name") for u in meta.get("universe", [])]
        if "BTC" in names:
            c = ctxs[names.index("BTC")]
            mark = safe_float(c.get("markPx")) or 0
            now.update({"hl_funding_apr": (safe_float(c.get("funding")) or 0) * 24 * 365 * 100,
                        "hl_oi_usd": (safe_float(c.get("openInterest")) or 0) * mark})
    except Exception as e:
        errors.append(f"Hyperliquid: {redact(e)[:120]}")
    return out, now, errors


def fetch_stablecoins():
    """DeFiLlama: tổng cung stablecoin toàn thị trường + USDT + USDC (USD)."""
    out, errors = {}, []
    for key, params in (("stables_total", None), ("usdt", {"stablecoin": 1}), ("usdc", {"stablecoin": 2})):
        try:
            rows = _jget("https://stablecoins.llama.fi/stablecoincharts/all", params, timeout=40)
            for row in rows[-400:]:
                d = _ts_day(row.get("date"))
                v = safe_float(((row.get("totalCirculatingUSD") or row.get("totalCirculating") or {}).get("peggedUSD")))
                if d and v:
                    out.setdefault(d, {})[key] = v
            time.sleep(1)
        except Exception as e:
            errors.append(f"DeFiLlama {key}: {redact(e)[:120]}")
    return out, errors


def fetch_coinbase_premium():
    """Coinbase BTC-USD so với OKX BTC-USDT (đã quy USDT→USD) theo giờ → trung bình mỗi ngày (%)."""
    cb = _jget("https://api.exchange.coinbase.com/products/BTC-USD/candles", {"granularity": 3600})
    okx = _jget(f"{OKX_API}/market/candles", {"instId": "BTC-USDT", "bar": "1H", "limit": 300}).get("data") or []
    try:
        usdt = {int(r[0]): safe_float(r[4]) for r in
                _jget("https://api.exchange.coinbase.com/products/USDT-USD/candles", {"granularity": 3600})}
    except Exception:
        usdt = {}
    cbm = {int(r[0]): safe_float(r[4]) for r in cb if len(r) >= 5}
    okm = {int(int(r[0]) / 1000): safe_float(r[4]) for r in okx if len(r) >= 5}
    hours = sorted(set(cbm) & set(okm))
    per_day, last = {}, None
    for h in hours:
        a, b, u = cbm[h], okm[h], usdt.get(h) or 1.0
        if a and b:
            p = (a / (b * u) - 1) * 100
            per_day.setdefault(_ts_day(h), []).append(p)
            last = (h, p)
    out = {d: {"cb_premium_pct": float(np.mean(v)), "cb_premium_hours": len(v)} for d, v in per_day.items() if len(v) >= 6}
    now = {"cb_premium_now": last[1], "cb_premium_hour_utc": datetime.fromtimestamp(last[0], tz=timezone.utc)
           .strftime("%Y-%m-%d %H:%M")} if last else {}
    return out, now


def fetch_fear_greed():
    rows = _jget("https://api.alternative.me/fng/", {"limit": 400, "format": "json"}).get("data") or []
    out = {}
    for r in rows:
        d, v = _ts_day(r.get("timestamp")), safe_float(r.get("value"))
        if d and v is not None:
            out[d] = {"fng": v, "fng_class": r.get("value_classification")}
    return out


def update_market_extra(status):
    data = _load_json(MARKET_FILE)
    daily = data.get("daily", {}) if isinstance(data.get("daily"), dict) else {}
    errors, now, done = [], {}, {}

    def merge(rows, label):
        n = 0
        for d, rec in rows.items():
            if is_day_key(d or ""):
                daily.setdefault(d, {}).update({k: v for k, v in rec.items() if v is not None})
                n += 1
        done[label] = n

    try:
        rows, nw, errs = fetch_btc_derivatives()
        merge(rows, "phái sinh")
        now.update(nw)
        errors += errs
    except Exception as e:
        errors.append(f"phái sinh: {redact(e)[:120]}")
    try:
        rows, errs = fetch_stablecoins()
        merge(rows, "stablecoin")
        errors += errs
    except Exception as e:
        errors.append(f"stablecoin: {redact(e)[:120]}")
    try:
        rows, nw = fetch_coinbase_premium()
        merge(rows, "coinbase premium")
        now.update(nw)
    except Exception as e:
        errors.append(f"Coinbase Premium: {redact(e)[:120]}")
    try:
        merge(fetch_fear_greed(), "sợ hãi & tham lam")
    except Exception as e:
        errors.append(f"Fear & Greed: {redact(e)[:120]}")
    cut = day_str(utc_today() - timedelta(days=DAILY_MAX_DAYS))
    daily = {d: daily[d] for d in sorted(daily) if d >= cut}
    _save_json(MARKET_FILE, {"updated_at_utc": utc_now().strftime("%Y-%m-%d %H:%M"), "daily": daily,
                             "now": now or data.get("now", {}), "errors": errors})
    status["market_extra"] = {"done": done, "errors": errors}
    _log(f"Phái sinh/stablecoin/premium/F&G: {done}" + (f"; lỗi {errors}" if errors else ""))


def market_frame(data):
    daily = data.get("daily", {}) if isinstance(data, dict) else {}
    rows = [{"date": parse_day(d), **{k: v for k, v in rec.items() if k != "fng_class"},
             "fng_class": rec.get("fng_class")} for d, rec in daily.items() if is_day_key(d) and isinstance(rec, dict)]
    if not rows:
        return pd.DataFrame(columns=["date"])
    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    for c in df.columns:
        if c not in ("date", "fng_class"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# =============================================================================
# ĐIỂM THỊ TRƯỜNG TỔNG HỢP (0–100) — giống cách chấm điểm NEAR
# =============================================================================
MARKET_WEIGHTS = {"On-chain (URPD · giá vốn · lãi/lỗ)": 0.30, "Dòng tiền tổ chức (ETF · Coinbase)": 0.20,
                  "Phái sinh (đòn bẩy)": 0.20, "Thanh khoản stablecoin": 0.15, "Tâm lý (Sợ hãi & Tham lam)": 0.15}
_LIGHT_VAL = {"🟢": 1, "🔴": -1, "🟡": 0}


def _mval(df, col, days_back=0):
    """Giá trị gần nhất ở/trước (ngày cuối - days_back)."""
    if df is None or df.empty or col not in df.columns:
        return None, None
    s = df[["date", col]].dropna()
    if s.empty:
        return None, None
    if days_back:
        s = s[s.date <= s.date.max() - timedelta(days=days_back)]
        if s.empty:
            return None, None
    return float(s[col].iloc[-1]), s.date.iloc[-1]


def market_signals(mdf, price):
    """Tín hiệu từ market_history: phái sinh, stablecoin, Coinbase premium, F&G. Trả dict nhóm → [(tên, val, chi tiết, cách đọc)]."""
    g = {"deriv": [], "liq": [], "flow": [], "mood": []}
    if mdf is None or mdf.empty:
        return g
    end = mdf.date.max()
    p_now, _ = _mval(mdf, "price")
    p_7, _ = _mval(mdf, "price", 7)
    ret7 = (p_now / p_7 - 1) * 100 if p_now and p_7 else None

    f = mdf[mdf.date > end - timedelta(days=3)]["funding_apr"].dropna() if "funding_apr" in mdf.columns else pd.Series(dtype=float)
    if len(f):
        v = float(f.mean())
        g["deriv"].append(("Funding perp BTC (TB 3 ngày, năm hóa)", -1 if v > 20 else (1 if v < -3 else 0),
                           f"{v:+.1f}%/năm" + (" — long quá đông" if v > 20 else " — short đông" if v < -3 else " — bình thường"),
                           "Funding cao = phe long trả phí lớn, đòn bẩy dồn một phía → dễ bị quét thanh lý. Âm = short đông (dễ ép short)."))
    if "oi_usd" in mdf.columns and "price" in mdf.columns and ret7 is not None:
        o = mdf.dropna(subset=["oi_usd", "price"])
        o = o.assign(oi_btc=o.oi_usd / o.price)
        a, _ = _mval(o, "oi_btc")
        b, _ = _mval(o, "oi_btc", 7)
        if a and b:
            ch = (a / b - 1) * 100
            val = (1 if ret7 > 0 else -1) if ch > 8 else 0
            g["deriv"].append(("Open interest 7 ngày (tính bằng BTC)", val,
                               f"{ch:+.1f}% · giá 7 ngày {ret7:+.1f}% · " + ("vị thế mới theo chiều tăng" if val > 0 else
                                                                          "vị thế mới theo chiều giảm" if val < 0 else "đòn bẩy không tăng rõ"),
                               "OI tăng mạnh cùng chiều giá = dòng tiền đòn bẩy mới đang đẩy giá theo hướng đó."))
    ls, _ = _mval(mdf, "long_short_ratio")
    if ls is not None:
        g["deriv"].append(("Tỷ lệ tài khoản long/short (OKX)", -1 if ls > 2.5 else (1 if ls < 0.9 else 0),
                           f"{ls:.2f}" + (" — đám đông nghiêng long" if ls > 2.5 else " — đám đông nghiêng short" if ls < 0.9 else ""),
                           "Chỉ báo ngược: khi số đông dồn một phía, giá hay đi ngược lại để quét họ."))
    if {"taker_buy", "taker_sell"}.issubset(mdf.columns):
        tk = mdf[mdf.date > end - timedelta(days=7)].dropna(subset=["taker_buy", "taker_sell"])
        if len(tk) >= 3 and tk.taker_sell.sum() > 0:
            ratio = float(tk.taker_buy.sum() / tk.taker_sell.sum())
            g["deriv"].append(("Lực mua chủ động / bán chủ động 7 ngày", 1 if ratio > 1.03 else (-1 if ratio < 0.97 else 0),
                               f"{ratio:.3f}", "> 1: lệnh mua thị trường áp đảo (chủ động đuổi giá lên)."))

    a, _ = _mval(mdf, "stables_total")
    b, _ = _mval(mdf, "stables_total", 7)
    if a and b:
        ch = (a / b - 1) * 100
        g["liq"].append(("Tổng cung stablecoin — 7 ngày", 1 if ch > 0.4 else (-1 if ch < -0.2 else 0),
                         f"{fmt_big_usd(a - b, signed=True)} ({ch:+.2f}%) · tổng {fmt_big_usd(a)}",
                         "Stablecoin phát hành thêm = tiền mới vào crypto, sẵn sàng mua. Bị đốt/rút = tiền rời thị trường."))
    a, _ = _mval(mdf, "stables_total")
    b, _ = _mval(mdf, "stables_total", 30)
    if a and b:
        ch = (a / b - 1) * 100
        g["liq"].append(("Tổng cung stablecoin — 30 ngày", 1 if ch > 1.5 else (-1 if ch < -0.5 else 0),
                         f"{fmt_big_usd(a - b, signed=True)} ({ch:+.2f}%)", "Xu hướng thanh khoản trung hạn."))

    v = premium_7d(mdf)
    if v is not None:
        g["flow"].append(("Coinbase Premium TB 7 ngày", 1 if v > 0.03 else (-1 if v < -0.03 else 0), f"{v:+.3f}%",
                          "Dương: giá trên Coinbase (nhà đầu tư Mỹ, tổ chức) cao hơn sàn khác → Mỹ đang mua mạnh hơn. Âm: Mỹ bán."))

    fg, _ = _mval(mdf, "fng")
    if fg is not None:
        fg7, _ = _mval(mdf, "fng", 7)
        cls = mdf.dropna(subset=["fng"]).fng_class.iloc[-1] if "fng_class" in mdf.columns else ""
        g["mood"].append(("Chỉ số Sợ hãi & Tham lam", 1 if fg <= 25 else (-1 if fg >= 75 else 0),
                          f"{fg:.0f} ({FNG_VI.get(cls, cls)})" + (f" · 7 ngày trước {fg7:.0f}" if fg7 is not None else ""),
                          "Chỉ báo ngược: sợ hãi cực độ thường gần vùng đáy ngắn hạn; tham lam cực độ thường gần đỉnh."))
    return g


FNG_VI = {"Extreme Fear": "Sợ hãi cực độ", "Fear": "Sợ hãi", "Neutral": "Trung lập", "Greed": "Tham lam",
          "Extreme Greed": "Tham lam cực độ"}


def market_composite(onchain_rows, mdf, price):
    """Điểm 0–100 có trọng số. onchain_rows = kết quả build_signals (gồm cả dòng ETF)."""
    ms = market_signals(mdf, price)
    oc = [(r[0], _LIGHT_VAL.get(r[1]), r[2], r[3]) for r in onchain_rows if "ETF" not in r[0] and r[1] in _LIGHT_VAL]
    etf = [(r[0], _LIGHT_VAL.get(r[1]), r[2], r[3]) for r in onchain_rows if "ETF" in r[0] and r[1] in _LIGHT_VAL]
    cats = [("On-chain (URPD · giá vốn · lãi/lỗ)", oc), ("Dòng tiền tổ chức (ETF · Coinbase)", etf + ms["flow"]),
            ("Phái sinh (đòn bẩy)", ms["deriv"]), ("Thanh khoản stablecoin", ms["liq"]),
            ("Tâm lý (Sợ hãi & Tham lam)", ms["mood"])]
    out, num, den = [], 0.0, 0.0
    for name, sigs in cats:
        sc = float(np.mean([s[1] for s in sigs])) if sigs else None
        out.append({"name": name, "score": sc, "weight": MARKET_WEIGHTS[name],
                    "signals": [{"label": s[0], "val": s[1], "detail": s[2], "why": s[3]} for s in sigs]})
        if sc is not None:
            num += MARKET_WEIGHTS[name] * sc
            den += MARKET_WEIGHTS[name]
    if not den:
        return None
    x = num / den
    score = 50 + 50 * x
    verdict = ("🟢 Tích cực" if score >= 65 else "🟢 Hơi tích cực" if score >= 55 else
               "🟡 Trung tính" if score > 45 else "🟠 Hơi tiêu cực" if score > 35 else "🔴 Tiêu cực")
    used = [c for c in out if c["score"] is not None]
    agree = sum(1 for c in used if x != 0 and np.sign(c["score"]) == np.sign(x))
    return {"score": score, "verdict": verdict, "categories": out, "agree": agree, "n_cats": len(used)}


def composite_outlook(comp, supports, resistances):
    s1 = f"{fmt_usd(supports[0]['low'])}–{fmt_usd(supports[0]['high'])}" if supports else None
    r1 = f"{fmt_usd(resistances[0]['low'])}–{fmt_usd(resistances[0]['high'])}" if resistances else None
    sc = comp["score"]
    if sc >= 55:
        head = "Dữ liệu tổng hợp đang **nghiêng về phía tích cực** cho 1–2 tuần tới."
        cond = (f"Xác nhận nếu giá giữ trên {s1} và vượt {r1}. " if s1 and r1 else "") + \
               (f"Mất hiệu lực nếu giá đóng ngày dưới {s1}." if s1 else "")
    elif sc <= 45:
        head = "Dữ liệu tổng hợp đang **nghiêng về phía tiêu cực** cho 1–2 tuần tới."
        cond = (f"Áp lực còn nếu giá không lấy lại được {r1}; vùng cần chú ý phía dưới {s1}. " if s1 and r1 else "") + \
               (f"Mất hiệu lực nếu giá vượt và giữ trên {r1}." if r1 else "")
    else:
        head = "Dữ liệu tổng hợp **trái chiều** — chưa có xu hướng rõ cho 1–2 tuần tới."
        cond = f"Chờ giá ra khỏi khoảng {s1} → {r1} để xác nhận hướng đi." if s1 and r1 else ""
    return head, cond, f"{comp['agree']}/{comp['n_cats']} nhóm cùng chiều với kết luận chung."


def premium_7d(mdf):
    """Coinbase Premium trung bình các ngày trong 7 ngày gần nhất (cùng cách tính ở mọi nơi)."""
    if mdf is None or mdf.empty or "cb_premium_pct" not in mdf.columns:
        return None
    s = mdf[mdf.date > mdf.date.max() - timedelta(days=7)]["cb_premium_pct"].dropna()
    return float(s.mean()) if len(s) >= 3 else None


def market_extra_brief(mdf):
    """1–2 dòng ngắn cho Telegram / Tổng quan."""
    if mdf is None or mdf.empty:
        return []
    parts = []
    f, _ = _mval(mdf, "funding_apr")
    if f is not None:
        parts.append(f"funding {f:+.1f}%/năm")
    ls, _ = _mval(mdf, "long_short_ratio")
    if ls is not None:
        parts.append(f"long/short {ls:.2f}")
    a, _ = _mval(mdf, "stables_total")
    b, _ = _mval(mdf, "stables_total", 7)
    if a and b:
        parts.append(f"stablecoin 7 ngày {fmt_big_usd(a - b, signed=True)}")
    pr = premium_7d(mdf)
    if pr is not None:
        parts.append(f"Coinbase Premium 7 ngày {pr:+.3f}%")
    fg, _ = _mval(mdf, "fng")
    if fg is not None:
        cls = mdf.dropna(subset=["fng"]).fng_class.iloc[-1] if "fng_class" in mdf.columns else ""
        parts.append(f"Sợ hãi & Tham lam {fg:.0f} ({FNG_VI.get(cls, cls)})")
    return [("📈 " + " · ".join(parts) + ".")] if parts else []


REPORT_FILE = "daily_report.json"


def etf_alt_frames(data):
    return {s: etf_frame(v) for s, v in ((data or {}).get("alts") or {}).items() if isinstance(v, dict)}


def etf_alts_line(frames):
    """🪙 1 dòng tóm tắt ETF altcoin: NEAR, LINK trước."""
    parts = []
    for sym in ETF_ALTS:
        stt = etf_stats(frames.get(sym)) if frames.get(sym) is not None and not frames[sym].empty else None
        if stt:
            parts.append(f"{sym} {fmt_big_usd(stt['last'], signed=True)} ({stt['date']:%d/%m}; 5 phiên "
                         f"{fmt_big_usd(stt['sum_5'], signed=True)})" if sym in ETF_ALT_FUNDS_FOR
                         else f"{sym} 5 phiên {fmt_big_usd(stt['sum_5'], signed=True)}")
    return ("🪙 ETF altcoin: " + " · ".join(parts) + ".") if parts else None


def _r(x, nd=0):
    v = safe_float(x)
    return None if v is None else round(v, nd) if nd else round(v)


def flow_summary(hist_frames, hist_lth, prices, sel, cmp_date, price):
    """BTC rời vùng giá cũ đi đâu (pure) — cùng logic với trang URPD."""
    cur, old = hist_frames.get(sel), hist_frames.get(cmp_date)
    if cur is None or old is None:
        return None
    d0, d1 = parse_day(cmp_date), parse_day(sel)
    closes = [p for k, p in prices.items() if d0 <= parse_day(k) <= d1 and p] or [price]
    zone_lo = float(np.floor(min(closes) * 0.98 / 1000) * 1000)
    zone_hi = float(np.ceil(max(closes) * 1.02 / 1000) * 1000)
    avg_px = float(np.mean(closes))
    bd = bucket_deltas(cur, old)
    if bd.empty:
        return None
    bd["mid"] = (bd.low + bd.high) / 2
    if hist_lth.get(sel) is not None and hist_lth.get(cmp_date) is not None:
        lb = bucket_deltas(hist_lth[sel], hist_lth[cmp_date])
        bd["lth"] = pd.Series(lb.delta.values, index=lb.low.values).reindex(bd.low).to_numpy(float)
    in_zone = (bd.high > zone_lo) & (bd.low < zone_hi)
    dep = bd[~in_zone & (bd.delta <= -50)].copy()
    dep["usd"] = -dep.delta * (avg_px - dep.mid)
    prof, loss = dep[dep.mid < zone_lo], dep[dep.mid >= zone_hi]
    top = dep.sort_values("delta").head(8)
    return {
        "from": cmp_date, "to": sel, "zone": [zone_lo, zone_hi],
        "moved_btc": _r(-dep.delta.sum()), "arrivals_in_zone_btc": _r(bd[in_zone].delta.sum()),
        "profit_btc": _r(-prof.delta.sum()), "profit_usd_est": _r(prof.usd.sum()),
        "loss_btc": _r(-loss.delta.sum()), "loss_usd_est": _r(-loss.usd.sum()),
        "lth_spent_btc_min": _r(-np.clip(dep["lth"].fillna(0), None, 0).sum()) if "lth" in dep.columns else None,
        "top_departures": [{"range": f"{a:.0f}-{h:.0f}", "delta": _r(dv),
                            "lth": _r(l) if "lth" in top.columns else None,
                            "state": "lãi" if m < zone_lo else "lỗ"}
                           for a, h, dv, m, l in zip(top.low, top.high, top.delta, top.mid,
                                                     top["lth"] if "lth" in top.columns else [None] * len(top))],
        "biggest_bucket_changes": [{"range": f"{a:.0f}-{h:.0f}", "delta": _r(dv)}
                                   for a, h, dv in bd.reindex(bd.delta.abs().sort_values(ascending=False).index)
                                   .head(6)[["low", "high", "delta"]].itertuples(index=False)],
    }


def etf_report(df, stt, source, prices):
    """Khối ETF trong daily_report.json."""
    if not stt:
        return None
    px = {parse_day(k): v for k, v in prices.items()}

    def btc_eq(row):
        p = px.get(row.date) or next((px[d] for d in sorted(px, reverse=True) if d <= row.date), None)
        return _r(row.total / p) if p else None

    last10 = [{"date": day_str(row.date), "usd": _r(row.total), "btc_est": btc_eq(row)}
              for row in df.tail(10).itertuples()]
    f5 = sorted(stt["funds_5"].items(), key=lambda kv: kv[1])
    return {"source": source, "latest_date": day_str(stt["date"]), "latest_usd": _r(stt["last"]),
            "funds_date": day_str(stt["funds_date"]),
            "sum_5d_usd": _r(stt["sum_5"]), "sum_20d_usd": _r(stt["sum_20"]), "streak": stt["streak"],
            "aum_usd": _r(stt["aum"]), "cum_usd": _r(stt["cum"]), "sessions": stt["sessions"],
            "funds_latest": {k: _r(v) for k, v in stt["funds_last"].items()},
            "top_in_5d": [{"fund": k, "usd": _r(v)} for k, v in reversed(f5[-3:]) if v > 0],
            "top_out_5d": [{"fund": k, "usd": _r(v)} for k, v in f5[:3] if v < 0],
            "last_10": last10}


def market_extra_report(mdf, mdata):
    if mdf is None or mdf.empty:
        return None

    def v(col, back=0, nd=2):
        x, d = _mval(mdf, col, back)
        return None if x is None else round(x, nd)
    st7 = v("stables_total", 0, 0)
    st7b = v("stables_total", 7, 0)
    pr = premium_7d(mdf)
    tk = mdf.tail(7).dropna(subset=["taker_buy", "taker_sell"]) if {"taker_buy", "taker_sell"}.issubset(mdf.columns) else None
    o = mdf.dropna(subset=["oi_usd", "price"]) if {"oi_usd", "price"}.issubset(mdf.columns) else None
    now = (mdata or {}).get("now") or {}
    return {
        "funding_apr_pct": v("funding_apr"), "hl_funding_apr_pct": _r(now.get("hl_funding_apr"), 2),
        "oi_usd": v("oi_usd", 0, 0), "oi_btc_chg_7d_pct": (round(((o.oi_usd / o.price).iloc[-1] / (o.oi_usd / o.price)
                                                                  .iloc[-8] - 1) * 100, 2) if o is not None and len(o) >= 8 else None),
        "long_short_ratio": v("long_short_ratio"),
        "taker_buy_sell_7d": round(float(tk.taker_buy.sum() / tk.taker_sell.sum()), 3) if tk is not None and len(tk) and tk.taker_sell.sum() else None,
        "stables_total_usd": st7, "stables_chg_7d_usd": (st7 - st7b) if st7 and st7b else None,
        "stables_chg_30d_usd": (st7 - v("stables_total", 30, 0)) if st7 and v("stables_total", 30, 0) else None,
        "usdt_usd": v("usdt", 0, 0), "usdc_usd": v("usdc", 0, 0),
        "cb_premium_7d_pct": round(pr, 4) if pr is not None else None,
        "cb_premium_now_pct": _r(now.get("cb_premium_now"), 4),
        "fear_greed": v("fng", 0, 0), "fear_greed_7d_ago": v("fng", 7, 0),
        "fear_greed_class": FNG_VI.get(mdf.dropna(subset=["fng"]).fng_class.iloc[-1], None) if "fng" in mdf.columns and mdf.fng.notna().any() else None,
        "errors": (mdata or {}).get("errors") or [],
    }


def build_daily_report(status):
    """Tóm tắt gọn mọi chỉ số chính vào daily_report.json — để báo cáo nhanh theo yêu cầu."""
    hist = _load_json(URPD_FILE)
    frames, lth, prices = {}, {}, {}
    for k, v in hist.items():
        if is_day_key(k) and isinstance(v, dict) and v.get("urpd"):
            try:
                frames[k] = records_to_urpd(v["urpd"])
                if v.get("urpd_lth"):
                    lth[k] = records_to_urpd(v["urpd_lth"])
                if safe_float(v.get("price")):
                    prices[k] = float(v["price"])
            except Exception:
                continue
    dates = sorted(frames)
    if not dates:
        return
    cur_date = dates[-1]
    cur = frames[cur_date]
    price = fetch_live_price() or prices.get(cur_date)
    onchain = merge_onchain_frames(_daily_file_frame(RESEARCH_FILE), _daily_file_frame(ONCHAIN_FILE))
    realized = _daily_file_frame(REALIZED_FILE)
    if not realized.empty and "realized_loss" in realized.columns:
        realized["realized_loss"] = realized["realized_loss"].abs()
        realized = realized.dropna(subset=["realized_profit", "realized_loss"])
    ath = safe_float(clean_secret(os.environ.get("ATH_USD", ""))) or DEFAULT_ATH

    def prev(n):
        k = day_str(parse_day(cur_date) - timedelta(days=n))
        return k if k in frames else None

    p1, p7 = prev(1), prev(7)
    sup, res = find_sr_zones(cur, price)
    etf_data = _load_json(ETF_FILE)
    etf_df = etf_frame(etf_data)
    etf = etf_stats(etf_df)
    alt_rep = {}
    for sym, adf in etf_alt_frames(etf_data).items():
        a_st = etf_stats(adf)
        alt_rep[sym] = etf_report(adf, a_st, "SoSoValue", {}) if a_st else None
        if alt_rep[sym]:
            alt_rep[sym].pop("last_10", None)
            alt_rep[sym]["last_5"] = [{"date": day_str(r_.date), "usd": _r(r_.total)} for r_ in adf.tail(5).itertuples()]
        err = ((etf_data.get("alts") or {}).get(sym) or {}).get("error")
        if err:
            alt_rep[sym] = {**(alt_rep[sym] or {}), "error": err}
    rows, g, r, verdict = build_signals(price, onchain, realized, cur, frames.get(p7), ath, etf)
    mdata = _load_json(MARKET_FILE)
    mdf = market_frame(mdata)
    comp = market_composite(rows, mdf, price)
    loss_pct, loss_d = latest_value(onchain, "supply_in_loss_percent")
    overhead = btc_in_range(cur, price, max(ath, price))

    # Vùng theo dõi (mặc định 57.800–67.300; đổi bằng secret WATCH_ZONE_LOW/HIGH)
    zlo = safe_float(clean_secret(os.environ.get("WATCH_ZONE_LOW", ""))) or WATCH_ZONE_DEFAULT[0]
    zhi = safe_float(clean_secret(os.environ.get("WATCH_ZONE_HIGH", ""))) or WATCH_ZONE_DEFAULT[1]
    zs = {d: btc_in_range(frames[d], zlo, zhi) for d in dates}
    peak_d = max(zs, key=zs.get)

    def zchg(n):
        k = day_str(parse_day(cur_date) - timedelta(days=n))
        older = [d for d in dates if d <= k]
        return _r(zs[cur_date] - zs[older[-1]]) if older else None

    rp = realized.iloc[-1] if not realized.empty else None
    news = []
    try:
        for n in fetch_news_radar(days=2, max_items=8):
            news.append({"title": n["title"], "title_vi": _translate_raw(n["title"]), "source": n["source"],
                         "category": n["category"], "time_utc": n["published"].strftime("%Y-%m-%d %H:%M")})
    except Exception:
        pass
    try:
        events = [{k: e[k] for k in ("date", "time", "event", "impact")}
                  for e in upcoming_btc_events(7, rows=fetch_economic_calendar_raw())]
    except Exception:
        events = []

    btc = {
        "price_live": _r(price), "urpd_latest_date": cur_date, "urpd_close": _r(prices.get(cur_date)),
        "history_days": len(dates),
        "supply_in_loss_pct": _r(loss_pct, 2), "supply_in_loss_date": day_str(loss_d) if loss_d else None,
        "supply_in_loss_pct_chg_1d": _r(loss_pct - value_days_before(onchain, "supply_in_loss_percent", loss_d, 1), 2)
        if loss_pct is not None and value_days_before(onchain, "supply_in_loss_percent", loss_d, 1) is not None else None,
        "sth_cost_basis": _r(latest_value(onchain, "sth_realized_price")[0]),
        "lth_cost_basis": _r(latest_value(onchain, "lth_realized_price")[0]),
        "mvrv": _r(latest_value(onchain, "mvrv")[0], 3), "nupl": _r(latest_value(onchain, "nupl")[0], 3),
        "mvrv_date": day_str(latest_value(onchain, "mvrv")[1]) if latest_value(onchain, "mvrv")[1] else None,
        "mvrv_is_estimate": bool("mvrv_is_est" in onchain.columns and onchain.dropna(subset=["mvrv"]).tail(1)
                                 .get("mvrv_is_est", pd.Series([False])).fillna(False).astype(bool).any()),
        "sopr_date": day_str(latest_value(onchain, "sopr")[1]) if latest_value(onchain, "sopr")[1] else None,
        "overhead_to_ath_btc": _r(overhead),
        "overhead_struct_chg_1d": _r(overhead - btc_in_range(frames[p1], price, max(ath, price))) if p1 else None,
        "realized_last": {"date": day_str(rp.date), "profit": _r(rp.realized_profit), "loss": _r(rp.realized_loss),
                          "net": _r(rp.realized_profit - rp.realized_loss)} if rp is not None else None,
        "realized_net_7d": _r((realized.tail(7).realized_profit - realized.tail(7).realized_loss).sum())
        if not realized.empty else None,
        "supports": [{"range": f"{z['low']:.0f}-{z['high']:.0f}", "btc": _r(z["btc"]), "dist_pct": _r(z["distance_pct"], 1)} for z in sup],
        "resistances": [{"range": f"{z['low']:.0f}-{z['high']:.0f}", "btc": _r(z["btc"]), "dist_pct": _r(z["distance_pct"], 1)} for z in res],
        "signals": [{"name": x[0], "light": x[1], "value": x[2]} for x in rows],
        "verdict": verdict, "greens": g, "reds": r,
        "flow_1d": flow_summary(frames, lth, prices, cur_date, p1, price) if p1 else None,
        "flow_7d": flow_summary(frames, lth, prices, cur_date, p7, price) if p7 else None,
        "watch_zone": {"lo": zlo, "hi": zhi, "btc": _r(zs[cur_date]),
                       "pct_supply": _r(zs[cur_date] / float(cur.btc_amount.sum()) * 100, 2),
                       "chg_1d": zchg(1), "chg_7d": zchg(7), "chg_30d": zchg(30),
                       "peak_date": peak_d, "peak_btc": _r(zs[peak_d]), "vs_peak": _r(zs[cur_date] - zs[peak_d])},
        "market": {k: (_r(v, 2) if isinstance(v, float) else v) for k, v in (status.get("market") or {}).items()},
        "etf": etf_report(etf_df, etf, (status.get("etf") or {}).get("source") or etf_data.get("source"), prices),
        "etf_alts": alt_rep,
        "market_extra": market_extra_report(mdf, mdata),
        "composite": ({"score": round(comp["score"]), "verdict": comp["verdict"],
                       "outlook": composite_outlook(comp, sup, res)[0], "conditions": composite_outlook(comp, sup, res)[1],
                       "agree": composite_outlook(comp, sup, res)[2],
                       "categories": [{"name": c["name"], "weight": c["weight"],
                                       "score": None if c["score"] is None else round(c["score"], 2),
                                       "signals": [f"{'🟢' if s['val'] > 0 else '🔴' if s['val'] < 0 else '🟡'} "
                                                   f"{s['label']}: {s['detail']}" for s in c["signals"]]}
                                      for c in comp["categories"]]} if comp else None),
        "events_7d": events, "news_48h": news,
        "data_errors": {k: (status.get(k) or {}).get("errors") for k in ("urpd", "research", "realized", "onchain", "etf",
                                                                          "market_extra")
                        if (status.get(k) or {}).get("errors")},
    }
    rep = _load_json(REPORT_FILE)
    rep.update({"generated_utc": utc_now().strftime("%Y-%m-%d %H:%M"), "btc": btc})
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1, default=str)
    _log("Báo cáo tổng hợp: đã ghi daily_report.json")


def run_update():
    _log(f"BTC URPD Monitor {APP_VERSION} — cập nhật dữ liệu")
    status = _load_json(STATUS_FILE)
    for k in ("urpd", "research", "realized", "onchain", "etf", "market_extra"):
        status.pop(k, None)
    if isinstance(status.get("telegram"), dict):
        status["telegram"].pop("error", None)
    status["fatal"] = []
    for step in (update_urpd, update_research, update_realized, update_onchain, update_market, update_etf,
                 update_market_extra, send_telegram_report,
                 build_daily_report):
        try:
            step(status)
        except Exception as e:  # một bước lỗi không làm hỏng các bước khác
            status.setdefault("fatal", []).append(redact(f"{step.__name__}: {e}")[:300])
            _log(f"{step.__name__} lỗi: {e}")
    status["last_run_utc"] = utc_now().strftime("%Y-%m-%d %H:%M")
    _save_json(STATUS_FILE, status)
    _log("Xong.")


# =============================================================================
# TIN TỨC & LỊCH KINH TẾ
# =============================================================================
NEWS_QUERIES = [
    ("Bitcoin", 'Bitcoin BTC'),
    ("Fed / lãi suất", 'Bitcoin Fed OR FOMC OR "interest rate"'),
    ("Lạm phát Mỹ", 'Bitcoin CPI OR PPI OR inflation'),
    ("ETF / dòng vốn", 'Bitcoin ETF OR "spot bitcoin ETF"'),
    ("Thanh khoản / trái phiếu", 'Bitcoin Treasury yields OR dollar OR liquidity'),
    ("Quy định crypto", 'Bitcoin SEC OR CFTC OR crypto regulation OR Clarity Act'),
    ("Địa chính trị / dầu", 'Bitcoin oil OR Iran OR Middle East OR geopolitics'),
]
NEGATIVE_TERMS = (
    "rate hike", "hike rates", "higher rates", "hawkish", "hot inflation", "inflation rises", "cpi rises",
    "ppi rises", "yield rises", "yields rise", "strong jobs", "oil surges", "oil rises", "risk-off", "selloff",
    "sell-off", "war", "wars", "iran", "outflows", "outflow", "crackdown", "ban", "bans", "banned", "lawsuit",
)
POSITIVE_TERMS = (
    "rate cut", "cut rates", "lower rates", "dovish", "cooling inflation", "inflation cools", "cpi falls",
    "ppi falls", "yield falls", "yields fall", "etf inflow", "inflows", "institutional demand", "approval",
    "regulatory clarity", "bullish", "buying",
)


@st.cache_data(ttl=6 * 60 * 60, show_spinner=False)
def fetch_news_radar(days=7, max_items=40):
    """Tin 7 ngày qua từ Google News RSS (không cần API key)."""
    now = utc_now()
    cutoff = now - timedelta(days=days)
    rows, seen = [], set()
    for category, query in NEWS_QUERIES:
        url = "https://news.google.com/rss/search?q=" + quote_plus(query) + "&hl=en-US&gl=US&ceid=US:en"
        try:
            r = requests.get(url, headers=HTTP_HEADERS, timeout=15)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception:
            continue
        for item in root.findall("./channel/item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not title or not link or title.lower() in seen:
                continue
            try:
                published = parsedate_to_datetime((item.findtext("pubDate") or "").strip()).astimezone(timezone.utc)
            except Exception:
                published = now
            if published < cutoff:
                continue
            seen.add(title.lower())
            src = item.find("source")
            rows.append({"title": title, "link": link, "category": category, "published": published,
                         "source": (src.text or "").strip() if src is not None else "Google News"})
    rows.sort(key=lambda x: x["published"], reverse=True)
    return rows[:max_items]


def _translate_raw(text):
    text = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", str(text or "")))).strip()
    if not text:
        return ""
    try:
        r = requests.get("https://translate.googleapis.com/translate_a/single",
                         params={"client": "gtx", "sl": "en", "tl": "vi", "dt": "t", "q": text[:1500]},
                         headers=HTTP_HEADERS, timeout=12)
        r.raise_for_status()
        return "".join(p[0] for p in r.json()[0] if p and p[0]).strip() or text
    except Exception:
        return text


@st.cache_data(ttl=86400, show_spinner=False)
def translate_batch(texts):
    """Dịch song song nhiều tiêu đề (cache 24h). Trả {gốc: bản dịch}."""
    texts = [t for t in dict.fromkeys(texts) if t]
    if not texts:
        return {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        return dict(zip(texts, ex.map(_translate_raw, texts)))


def _contains_term(text, term):
    return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text) is not None


def news_tag(title, category):
    """Nhãn tự động theo từ khóa — chỉ để lọc nhanh, KHÔNG dùng để chấm điểm."""
    t = title.lower()
    if any(_contains_term(t, x) for x in NEGATIVE_TERMS):
        direction = "🔴 Có thể bất lợi"
    elif any(_contains_term(t, x) for x in POSITIVE_TERMS):
        direction = "🟢 Có thể thuận lợi"
    else:
        direction = "🟡 Trung tính"
    impact = "Trực tiếp" if category in {"Fed / lãi suất", "Lạm phát Mỹ", "ETF / dòng vốn"} else "Gián tiếp"
    return impact, direction


# Lịch dự phòng (cập nhật tay) — chỉ dùng khi không lấy được lịch tự động.
STATIC_EVENTS = [
    {"date": "2026-11-10", "time": "08:30 ET", "event": "CPI Mỹ tháng 10", "impact": "🔴🔴 Rất cao",
     "why": "Tín hiệu lạm phát → kỳ vọng lãi suất Fed → lợi suất/USD → BTC."},
    {"date": "2026-12-09", "time": "14:00 ET", "event": "FOMC — quyết định lãi suất", "impact": "🔴🔴 Rất cao",
     "why": "Quyết định/thông điệp Fed tác động trực tiếp tới thanh khoản và khẩu vị rủi ro."},
]
_VERY_HIGH_KEYS = ("cpi", "fomc", "federal funds", "fed chair", "non-farm", "nonfarm", "pce", "interest rate")


def _event_why(title):
    t = title.lower()
    if any(k in t for k in ("cpi", "pce", "ppi", "inflation")):
        return "Tín hiệu lạm phát → kỳ vọng lãi suất Fed → lợi suất/USD → BTC."
    if any(k in t for k in ("fomc", "federal funds", "fed", "interest rate")):
        return "Quyết định/thông điệp Fed tác động trực tiếp tới thanh khoản và khẩu vị rủi ro."
    if any(k in t for k in ("payroll", "employment", "jobless", "unemployment")):
        return "Thị trường lao động ảnh hưởng kỳ vọng chính sách Fed."
    if any(k in t for k in ("gdp", "retail", "pmi", "ism")):
        return "Tín hiệu tăng trưởng kinh tế Mỹ; ảnh hưởng gián tiếp qua lợi suất và USD."
    return "Dữ liệu vĩ mô Mỹ có thể làm USD/lợi suất biến động, gián tiếp tác động BTC."


def fetch_economic_calendar_raw():
    rows = []
    for url in ("https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                "https://nfs.faireconomy.media/ff_calendar_nextweek.json"):
        try:
            r = requests.get(url, headers=HTTP_HEADERS, timeout=15)
            if r.status_code == 200 and isinstance(r.json(), list):
                rows.extend(x for x in r.json() if isinstance(x, dict))
        except Exception:
            continue
    return rows


@st.cache_data(ttl=6 * 60 * 60, show_spinner=False)
def fetch_economic_calendar():
    return fetch_economic_calendar_raw()


def upcoming_btc_events(days=7, rows=None):
    """Sự kiện vĩ mô Mỹ (High/Medium) trong N ngày tới, giờ Việt Nam. Lỗi nguồn → lịch dự phòng."""
    if rows is None:
        rows = fetch_economic_calendar()
    now = utc_now()
    today, end = now.astimezone(VN_TZ).date(), now.astimezone(VN_TZ).date() + timedelta(days=days)
    out, seen = [], set()
    for e in rows:
        try:
            if str(e.get("country", "")).upper() != "USD":
                continue
            imp = str(e.get("impact", "")).lower()
            if imp not in ("high", "medium"):
                continue
            dt = datetime.fromisoformat(str(e.get("date", "")).replace("Z", "+00:00"))
            dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            if dt < now - timedelta(hours=2):
                continue
            local = dt.astimezone(VN_TZ)
            title = str(e.get("title", "")).strip()
            if not title or not (today <= local.date() <= end) or (local, title) in seen:
                continue
            seen.add((local, title))
            extra = [f"{lbl} {e.get(k)}" for k, lbl in (("forecast", "dự báo"), ("previous", "kỳ trước")) if e.get(k)]
            out.append({
                "date": day_str(local), "time": local.strftime("%H:%M") + " giờ VN",
                "event": title + (f" ({', '.join(extra)})" if extra else ""),
                "impact": ("🔴🔴 Rất cao" if any(k in title.lower() for k in _VERY_HIGH_KEYS) else "🟠 Cao")
                if imp == "high" else "🟡 Vừa",
                "why": _event_why(title), "sort": local, "source": "Lịch kinh tế tự động (ForexFactory)",
            })
        except Exception:
            continue
    if out:
        return sorted(out, key=lambda x: x["sort"])
    for e in STATIC_EVENTS:
        d = parse_day(e["date"])
        if today <= d <= end:
            out.append({**e, "source": "Lịch cấu hình sẵn (STATIC_EVENTS)"})
    return out


# =============================================================================
# LỚP DỮ LIỆU CỦA DASHBOARD (chỉ đọc, có cache)
# =============================================================================
def _secret(name, default=""):
    try:
        v = st.secrets.get(name, None)
    except Exception:
        v = None
    return str(v).strip() if v else os.environ.get(name, default)


def read_data_file(filename):
    """Đọc file dữ liệu: ưu tiên bản mới nhất trên GitHub (do Actions commit), sau đó file local."""
    repo = clean_secret(_secret("GITHUB_REPO"))
    token = clean_secret(_secret("GITHUB_TOKEN"))
    branch = clean_secret(_secret("GITHUB_BRANCH", "main")) or "main"
    err = None
    if repo:
        try:
            r = None
            if token:
                r = requests.get(f"https://api.github.com/repos/{repo}/contents/{filename}",
                                 headers={"Accept": "application/vnd.github.raw+json",
                                          "Authorization": f"Bearer {token}",
                                          "X-GitHub-Api-Version": "2022-11-28"},
                                 params={"ref": branch}, timeout=30)
                if r.status_code in (401, 403):
                    err = "GITHUB_TOKEN trên Streamlit không hợp lệ — đang đọc bản public"
                    r = None
            if r is None:  # repo public: đọc trực tiếp, không cần token
                r = requests.get(f"https://raw.githubusercontent.com/{repo}/{branch}/{filename}",
                                 headers=HTTP_HEADERS, params={"t": int(time.time() // 300)}, timeout=30)
            if r.status_code == 404:
                err = "GitHub chưa có file"
            else:
                r.raise_for_status()
                data = json.loads(r.text)
                return (data if isinstance(data, dict) else {}), f"GitHub {repo}@{branch}" + (f" ({err})" if err else "")
        except Exception as e:
            err = f"lỗi GitHub: {redact(e)[:150]}"
    try:
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
        return (data if isinstance(data, dict) else {}), "file trong bản deploy" + (f" ({err})" if err else "")
    except FileNotFoundError:
        return {}, "chưa có dữ liệu" + (f" ({err})" if err else "")
    except Exception as e:
        return {}, f"lỗi đọc file: {e}"


@st.cache_resource(ttl=900, show_spinner="Đang tải lịch sử URPD…")
def get_urpd_store():
    """Toàn bộ snapshot URPD đã chuẩn hóa — dùng chung, KHÔNG được sửa trực tiếp."""
    raw, source = read_data_file(URPD_FILE)
    frames, prices, frames_lth = {}, {}, {}
    for k, v in raw.items():
        if not is_day_key(k) or not isinstance(v, dict) or not v.get("urpd"):
            continue
        try:
            frames[k] = records_to_urpd(v["urpd"])
            if v.get("urpd_lth"):
                try:
                    frames_lth[k] = records_to_urpd(v["urpd_lth"])
                except Exception:
                    pass
            p = safe_float(v.get("price"))
            if p:
                prices[k] = p
        except Exception:
            continue
    dates = sorted(frames)
    return {"frames": frames, "prices": prices, "dates": dates, "source": source, "frames_lth": frames_lth,
            "version": f"{dates[-1] if dates else 'none'}|{len(dates)}|{utc_now():%H%M}"}


def _daily_to_frame(data):
    daily = data.get("daily", {}) if isinstance(data, dict) else {}
    rows = [{"date": parse_day(d), **rec} for d, rec in daily.items() if is_day_key(d) and isinstance(rec, dict)]
    if not rows:
        return pd.DataFrame(columns=["date"])
    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    for c in df.columns:
        if c != "date":
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


@st.cache_data(ttl=900, show_spinner=False)
def load_daily_file(filename):
    data, source = read_data_file(filename)
    return _daily_to_frame(data), source, (data.get("updated_at_utc") if isinstance(data, dict) else None)


def load_onchain():
    rb, _, _ = load_daily_file(RESEARCH_FILE)
    bg, _, _ = load_daily_file(ONCHAIN_FILE)
    keep = ["date"] + [c for c in ("supply_in_loss", "supply_in_loss_percent", "sth_realized_price",
                                   "lth_realized_price") if c in rb.columns]
    return merge_onchain_frames(rb[keep] if not rb.empty else rb, bg)


def load_realized():
    df, _, _ = load_daily_file(REALIZED_FILE)
    if df.empty or "realized_profit" not in df.columns or "realized_loss" not in df.columns:
        return pd.DataFrame(columns=["date", "realized_profit", "realized_loss", "btc_price"])
    df = df.dropna(subset=["realized_profit", "realized_loss"]).copy()
    df["realized_loss"] = df["realized_loss"].abs()
    if "btc_price" not in df.columns:
        df["btc_price"] = np.nan
    return df.reset_index(drop=True)


@st.cache_data(ttl=900, show_spinner=False)
def load_status():
    data, _ = read_data_file(STATUS_FILE)
    return data


@st.cache_data(ttl=300, show_spinner=False)
def live_price():
    return fetch_live_price()


@st.cache_data(ttl=900, show_spinner=False)
def live_market():
    """CoinGecko hay chặn IP dùng chung của Streamlit Cloud (lỗi 429) →
    khi lỗi thì dùng số liệu GitHub Actions đã lấy ở lần chạy gần nhất."""
    try:
        return fetch_market_overview(), None
    except Exception as e:
        saved = load_status().get("market")
        if isinstance(saved, dict) and saved.get("total_mcap"):
            return saved, None
        return None, str(e)


@st.cache_data(ttl=3600, show_spinner=False)
def live_bitview_latest():
    """Dự phòng khi history đã cũ (Actions chưa chạy): lấy URPD mới nhất trực tiếp, KHÔNG lưu."""
    for d in (utc_today(), utc_today() - timedelta(days=1)):
        try:
            frame, _, close, _ = fetch_bitview_urpd(day_str(d))
            if safe_float(close):
                return day_str(d), urpd_to_records(frame), float(close)
        except Exception:
            continue
    return None


@st.cache_data(ttl=900, show_spinner=False)
def daily_metrics_table(version, ath):
    """Chỉ số URPD theo từng ngày (tính tại giá đóng cửa của chính ngày đó)."""
    store = get_urpd_store()
    rows = []
    for d in store["dates"]:
        p = store["prices"].get(d)
        if p:
            rows.append({"date": parse_day(d), "price": p, **snapshot_metrics(store["frames"][d], p, ath)})
    return pd.DataFrame(rows)


def current_snapshot(store):
    """(frame, ngày, giá snapshot, nguồn) — file tải lên > history > Bitview trực tiếp."""
    up = st.session_state.get("uploaded_urpd")
    if up is not None:
        return up["frame"], up["name"], None, "File tải lên"
    dates = store["dates"]
    if not dates or parse_day(dates[-1]) < utc_today():
        # Ngày UTC mới đã bắt đầu (07:00 giờ VN) nhưng GitHub Actions chưa chạy → lấy tạm trực tiếp từ Bitview.
        live = live_bitview_latest()
        if live and (not dates or live[0] > dates[-1]):
            return (records_to_urpd(live[1]), live[0], live[2],
                    "Bitview trực tiếp — số tạm tính trong ngày, sẽ được lưu ở lần chạy GitHub Actions tới")
    if not dates:
        return None, None, None, "Chưa có dữ liệu"
    d = dates[-1]
    return store["frames"][d], d, store["prices"].get(d), store["source"]


def frame_on(store, day, days_back):
    key = day_str(parse_day(day) - timedelta(days=days_back))
    return store["frames"].get(key), key


# =============================================================================
# GIAO DIỆN — tiện ích
# =============================================================================
def _st_version():
    try:
        return tuple(int(x) for x in re.findall(r"\d+", st.__version__)[:2])
    except Exception:
        return (0, 0)


STRETCH = {"width": "stretch"} if _st_version() >= (1, 50) else {"use_container_width": True}
CHART_BG = "#0b0f14"
GREEN, RED, AMBER, CYAN, GREY = "#10b981", "#ef4444", "#f59e0b", "#22d3ee", "#94a3b8"


def inject_css():
    st.markdown("""
<style>
.block-container {padding-top: 2rem; padding-bottom: 2rem; max-width: 1500px;}
[data-testid="stMetric"] {
    background: linear-gradient(135deg, rgba(30,41,59,.95), rgba(15,23,42,.95));
    border: 1px solid rgba(148,163,184,.22); padding: 14px 18px; border-radius: 14px;
}
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] * {font-size: .88rem; color: #cbd5e1 !important;}
[data-testid="stMetricValue"] {font-size: 1.5rem; color: #f8fafc !important;}
section[data-testid="stSidebar"] input, section[data-testid="stSidebar"] [data-baseweb="input"] > div,
section[data-testid="stSidebar"] button {background: #1f2937 !important; color: #f8fafc !important;}
section[data-testid="stSidebar"] {background: linear-gradient(180deg,#111827 0%,#0f172a 100%);}
section[data-testid="stSidebar"] * {color: #e5e7eb;}
h1 {letter-spacing: -.035em;} h2, h3 {letter-spacing: -.02em;}
div[data-testid="stDataFrame"] {border-radius: 12px; overflow: hidden;}
</style>""", unsafe_allow_html=True)


def _esc(text):
    """Escape '$' để Streamlit không hiểu nhầm số tiền là công thức LaTeX."""
    return re.sub(r"(?<!\\)\$", r"\\$", str(text))


def mk(text, **kw):
    st.markdown(_esc(text), **kw)


def cap(text, **kw):
    st.caption(_esc(text), **kw)


def style_fig(fig, height=480, hovermode="x unified"):
    fig.update_layout(
        template="plotly_dark", height=height, paper_bgcolor=CHART_BG, plot_bgcolor=CHART_BG,
        margin=dict(l=55, r=40, t=50, b=40), hovermode=hovermode,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, bgcolor="rgba(0,0,0,0)"),
    )
    return fig


def show_fig(fig, key):
    st.plotly_chart(fig, key=key, config={"displaylogo": False}, theme=None, **STRETCH)


def show_df(df, **kw):
    st.dataframe(df, hide_index=True, **STRETCH, **kw)


def ath_value():
    return float(st.session_state.get("ath", DEFAULT_ATH))


def render_sidebar():
    with st.sidebar:
        mk("### ⚙️ Thiết lập")
        st.number_input("ATH (USD)", value=DEFAULT_ATH, step=100.0, key="ath",
                        help="Dùng cho chỉ số 'Cung giá hiện tại → ATH'.")

        mk("### 🗂️ Dữ liệu")
        status = load_status()
        store = get_urpd_store()
        if status.get("last_run_utc"):
            last = datetime.strptime(status["last_run_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            cap(f"GitHub Actions cập nhật lần cuối: **{last.astimezone(VN_TZ):%d/%m %H:%M}** giờ VN")
            for step in ("urpd", "research", "realized", "onchain", "etf", "market_extra"):
                errs = (status.get(step) or {}).get("errors") or []
                if errs:
                    cap(f"⚠️ {step}: {redact(errs[-1])[:120]}")
        else:
            st.info("Chưa thấy update_status.json — GitHub Actions chưa chạy lần nào. "
                    "Xem hướng dẫn ở đầu file app.py.")
        cap(f"URPD: {len(store['dates'])} ngày • mới nhất {store['dates'][-1] if store['dates'] else 'N/A'} "
                   f"• nguồn {store['source']}")
        if st.button("🔄 Tải lại dữ liệu mới nhất", **STRETCH):
            st.cache_data.clear()
            st.cache_resource.clear()
            st.rerun()

        with st.expander("📂 Dùng URPD từ file CSV/XLSX"):
            up = st.file_uploader("File URPD", type=["csv", "xlsx"], label_visibility="collapsed")
            if up is not None:
                try:
                    raw = pd.read_csv(up) if up.name.lower().endswith(".csv") else pd.read_excel(up)
                    st.session_state["uploaded_urpd"] = {"frame": normalize_urpd(raw), "name": up.name}
                    cap(f"Đang dùng {up.name} cho trang Tổng quan/URPD.")
                except Exception as e:
                    st.error(f"Lỗi file: {e}")
                    st.session_state.pop("uploaded_urpd", None)
            else:
                st.session_state.pop("uploaded_urpd", None)
        cap(f"BTC URPD Monitor {APP_VERSION}")


def stale_warning(label, day, max_days):
    if day is None:
        return
    d = parse_day(day) if isinstance(day, str) else day
    age = (utc_today() - d).days
    if age > max_days:
        st.warning(f"⚠️ {label} mới nhất là {day_str(d)} (cũ {age} ngày) — kiểm tra GitHub Actions.")


@st.cache_data(ttl=900, show_spinner=False)
def load_market():
    data, source = read_data_file(MARKET_FILE)
    return market_frame(data), source, (data.get("now") or {}) if isinstance(data, dict) else {}, \
        (data.get("updated_at_utc") if isinstance(data, dict) else None)


def _cat_light(sc):
    return "⚪" if sc is None else ("🟢" if sc > 0.2 else ("🔴" if sc < -0.2 else "🟡"))


def render_composite(comp, supports, resistances):
    st.subheader("🧭 Điểm thị trường tổng hợp")
    if not comp:
        cap("Chưa đủ dữ liệu để chấm điểm (cần chạy workflow để lấy phái sinh/stablecoin/tâm lý).")
        return
    head, cond, agree = composite_outlook(comp, supports, resistances)
    left, right = st.columns([1, 2])
    with left:
        g = go.Figure(go.Indicator(
            mode="gauge+number", value=comp["score"], number={"suffix": "/100", "font": {"size": 34}, "valueformat": ".0f"},
            gauge={"axis": {"range": [0, 100]}, "bar": {"color": "#f8fafc", "thickness": 0.25},
                   "steps": [{"range": [0, 35], "color": "#7f1d1d"}, {"range": [35, 45], "color": "#9a3412"},
                             {"range": [45, 55], "color": "#78716c"}, {"range": [55, 65], "color": "#166534"},
                             {"range": [65, 100], "color": "#15803d"}]}))
        g.update_layout(height=220, margin=dict(l=20, r=20, t=10, b=0), paper_bgcolor=CHART_BG, font_color="#e5e7eb")
        show_fig(g, "mkt_gauge")
    with right:
        mk(f"### {comp['verdict']}")
        mk(head)
        if cond:
            mk(cond)
        cap(agree + " Điểm = 50 + 50 × trung bình có trọng số các nhóm (mỗi tín hiệu +1 / 0 / −1). "
            "Chỉ là bản tổng hợp dữ liệu, không phải dự báo hay khuyến nghị mua/bán.")
    cols = st.columns(len(comp["categories"]))
    for col, c in zip(cols, comp["categories"]):
        with col:
            mk(f"**{_cat_light(c['score'])} {c['name']}**  \ntrọng số {int(c['weight'] * 100)}%"
               + (f" · điểm {c['score']:+.2f}" if c["score"] is not None else " · chưa có dữ liệu"))
            for s in c["signals"]:
                icon = "🟢" if s["val"] > 0 else ("🔴" if s["val"] < 0 else "🟡")
                mk(f"<small>{icon} {_esc(s['label'])}: {_esc(s['detail'])}</small>", unsafe_allow_html=True)


# =============================================================================
# TRANG 1 — TỔNG QUAN
# =============================================================================
def page_overview():
    ath = ath_value()
    store = get_urpd_store()
    frame, cur_date, snap_price, source = current_snapshot(store)
    price = live_price() or snap_price
    market, market_err = live_market()
    onchain, realized = load_onchain(), load_realized()
    events = upcoming_btc_events(7)
    etf_df, _, _, _, etf_alts = load_etf()
    etf = etf_stats(etf_df)
    etf_alt_df = {s: v[0] for s, v in etf_alts.items()}

    st.title("Bitcoin — bảng theo dõi hằng ngày")
    cap(f"URPD {cur_date or 'N/A'} ({source}) • giá trực tiếp Coinbase/CoinGecko • on-chain: ResearchBitcoin + BGeometrics")
    if is_day_key(cur_date or ""):
        stale_warning("Snapshot URPD", cur_date, 2)
    _, loss_date = latest_value(onchain, "supply_in_loss_percent")
    stale_warning("Dữ liệu Supply in Loss", loss_date, 3)
    if frame is None:
        st.error("Chưa có dữ liệu URPD. Hãy chạy workflow **Update BTC data** trên GitHub (tab Actions → Run workflow).")
    if not price:
        st.error("Không lấy được giá BTC.")
        return

    prev_frame = frame_on(store, cur_date, 1)[0] if is_day_key(cur_date or "") else None
    f7 = frame_on(store, cur_date, 7)[0] if is_day_key(cur_date or "") else None
    supports, resistances = find_sr_zones(frame, price) if frame is not None else ([], [])
    chg = market.get("btc_change_24h") if market else None

    st.subheader("📰 Hôm nay có gì thay đổi")
    mdf = load_market()[0]
    brief = daily_brief(price, chg, onchain, realized, cur_date, frame, prev_frame, supports, resistances, events, etf,
                        etf_alt_df) + market_extra_brief(mdf)
    mk("\n".join(f"- {b}" for b in brief) if brief else "Chưa đủ dữ liệu.")

    sig_rows = build_signals(price, onchain, realized, frame, f7, ath, etf)[0]
    render_composite(market_composite(sig_rows, mdf, price), supports, resistances)

    # ---- Thẻ số chính
    c = st.columns(4)
    c[0].metric("Giá BTC", fmt_usd(price), f"{chg:+.2f}% 24h" if chg is not None else None)
    c[1].metric("Cách ATH", f"{(price / ath - 1) * 100:+.1f}%", f"ATH {fmt_usd(ath)}", delta_color="off")
    loss_pct, _ = latest_value(onchain, "supply_in_loss_percent")
    loss_btc, _ = latest_value(onchain, "supply_in_loss")
    l1 = value_days_before(onchain, "supply_in_loss_percent", loss_date, 1)
    c[2].metric("Cung đang lỗ", f"{loss_pct:.2f}%" if loss_pct is not None else "N/A",
                f"{loss_pct - l1:+.2f} điểm % (1D)" if loss_pct is not None and l1 is not None else None,
                delta_color="inverse", help="ResearchBitcoin, ngày hoàn chỉnh gần nhất.")
    overhead = btc_in_range(frame, price, max(ath, price)) if frame is not None else None
    struct1d = (overhead - btc_in_range(prev_frame, price, max(ath, price))) if prev_frame is not None and overhead is not None else None
    c[3].metric("Cung giá hiện tại → ATH (URPD)", fmt_btc(overhead),
                f"{struct1d:+,.0f} BTC cấu trúc 1D" if struct1d is not None else None, delta_color="inverse",
                help="Δ tính tại CÙNG mức giá hiện tại cho cả hai snapshot → loại ảnh hưởng do giá di chuyển.")

    c = st.columns(4)
    sth, _ = latest_value(onchain, "sth_realized_price")
    lth, _ = latest_value(onchain, "lth_realized_price")
    c[0].metric("Giá vốn STH", fmt_usd(sth), f"giá {(price / sth - 1) * 100:+.1f}%" if sth else None)
    c[1].metric("Giá vốn LTH", fmt_usd(lth), f"giá {(price / lth - 1) * 100:+.1f}%" if lth else None)
    mvrv, mvrv_d = latest_value(onchain, "mvrv")
    nupl, _ = latest_value(onchain, "nupl")
    is_est = bool("mvrv_is_est" in onchain.columns and mvrv_d is not None
                  and onchain.loc[onchain.date == mvrv_d, "mvrv_is_est"].fillna(False).astype(bool).any())
    c[2].metric("MVRV · NUPL" + (" (ước tính)" if is_est else ""), f"{mvrv:.2f}" if mvrv is not None else "N/A",
                f"NUPL {nupl:.2f}" if nupl is not None else None, delta_color="off",
                help=("BGeometrics miễn phí trễ ~7 ngày → các ngày gần đây ước tính từ URPD "
                      "(giá / giá vốn trung bình toàn thị trường, hiệu chỉnh theo BGeometrics)." if is_est else None))
    sopr7 = None
    if "sopr" in onchain.columns and onchain["sopr"].notna().sum() >= 3:
        sopr7 = float(onchain["sopr"].dropna().tail(7).mean())
    c[3].metric("SOPR TB 7 ngày", f"{sopr7:.3f}" if sopr7 is not None else "N/A",
                ("> 1: bán có lãi" if sopr7 >= 1 else "< 1: bán lỗ") if sopr7 is not None else None, delta_color="off")

    if loss_btc and overhead:
        gap = (overhead - loss_btc) / loss_btc * 100
        msg = (f"🔍 Đối chiếu chéo: URPD trên giá hiện tại {fmt_btc(overhead)} vs Supply in Loss {fmt_btc(loss_btc)} "
               f"→ lệch {gap:+.1f}%. Lệch vài % là bình thường (giá trực tiếp vs giá đóng cửa ngày).")
        if abs(gap) > 10:
            st.warning(msg + " Lệch > 10%: nên kiểm tra lại nguồn dữ liệu.")
        else:
            cap(msg)

    if market:
        mk("#### 🌐 Vốn hóa thị trường")
        if market.get("fetched_at_utc"):
            t = datetime.strptime(market["fetched_at_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            cap(f"CoinGecko đang giới hạn lượt gọi — hiển thị số liệu lúc {t.astimezone(VN_TZ):%d/%m %H:%M} giờ VN.")
        m = st.columns(4)
        m[0].metric("Tổng vốn hóa", fmt_big_usd(market["total_mcap"]),
                    f"{market['total_mcap_change_24h']:+.2f}%" if market["total_mcap_change_24h"] is not None else None)
        m[1].metric("Vốn hóa BTC", fmt_big_usd(market["btc_mcap"]),
                    f"{market['btc_change_24h']:+.2f}%" if market["btc_change_24h"] is not None else None)
        m[2].metric("BTC Dominance (loại USDT+USDC)",
                    f"{market['adjusted_btc_dom']:.2f}%" if market["adjusted_btc_dom"] else "N/A")
        m[3].metric("Altcoin (trừ USDT+USDC)", fmt_big_usd(market["alt_ex_stables"]),
                    f"{market['alt_change_24h']:+.2f}%" if market["alt_change_24h"] is not None else None)
    elif market_err:
        cap(f"Chưa lấy được vốn hóa thị trường: {market_err[:150]}")

    if etf:
        mk("#### 🏦 Dòng tiền ETF BTC giao ngay")
        render_etf_cards(etf, _etf_btc_equiv(etf_df.tail(1), store).iloc[-1])
        alt_line = etf_alts_line(etf_alt_df)
        if alt_line:
            mk(alt_line)
        cap("Chi tiết từng quỹ, NEAR/LINK/ETH/SOL/XRP ở trang **Dòng tiền ETF**.")

    # ---- Hỗ trợ / kháng cự
    st.subheader("🎯 Vùng hỗ trợ / kháng cự tự động (cụm giá vốn URPD)")
    cap("Mỗi vùng rộng $3.000, chọn từ các cụm BTC có giá vốn dày nhất trong ±35% quanh giá hiện tại. "
               "Cụm dưới giá = nhiều người đang lãi ít (hay giữ/mua thêm khi về hòa vốn); "
               "cụm trên giá = nhiều người đang lỗ chờ hòa vốn (dễ bán khi giá hồi lên).")

    def zones_df(zones):
        return pd.DataFrame([{
            "Vùng giá": f"{fmt_usd(z['low'])} – {fmt_usd(z['high'])}", "BTC giá vốn": fmt_btc(z["btc"]),
            "% tổng cung": f"{z['share_pct']:.2f}%", "Cách giá": f"{z['distance_pct']:+.1f}%"} for z in zones])

    a, b = st.columns(2)
    with a:
        mk("**🟢 Hỗ trợ (dưới giá)**")
        if supports:
            show_df(zones_df(supports))
        else:
            cap("Chưa có dữ liệu.")
    with b:
        mk("**🔴 Kháng cự (trên giá)**")
        if resistances:
            show_df(zones_df(resistances))
        else:
            cap("Chưa có dữ liệu.")
    for line in scenario_text(price, supports, resistances):
        mk(f"- {line}")

    # ---- Tín hiệu
    st.subheader("🚦 Tín hiệu tổng hợp")
    rows, g, r, verdict = build_signals(price, onchain, realized, frame, f7, ath, etf)
    mk(f"### {verdict}")
    cap(f"{g} 🟢 · {len(rows) - g - r} 🟡 · {r} 🔴 trên {len(rows)} tín hiệu. "
               "Chỉ báo này đếm tín hiệu, không phải xác suất, dự báo hay khuyến nghị mua/bán.")
    if rows:
        show_df(pd.DataFrame(rows, columns=["Tín hiệu", "Đèn", "Giá trị", "Cách đọc"]))

    # ---- Sự kiện
    st.subheader("⏰ Sự kiện vĩ mô Mỹ 7 ngày tới")
    if events:
        cap(f"Nguồn: {events[0]['source']}")
        show_df(pd.DataFrame([{"Ngày": e["date"], "Giờ": e["time"], "Sự kiện": e["event"],
                               "Mức độ": e["impact"], "Vì sao quan trọng": e["why"]} for e in events]))
    else:
        cap("Không có sự kiện quan trọng trong 7 ngày tới.")


# =============================================================================
# TRANG 2 — URPD
# =============================================================================
def urpd_figure(frame, price, label, cmp_frame=None, cmp_label=None, sth=None, lth=None,
                supports=(), resistances=(), ath=DEFAULT_ATH, lth_delta=None):
    """Biểu đồ URPD; khi có snapshot so sánh thì thêm hàng dưới hiển thị Δ BTC từng cột (+ Δ của LTH)."""
    plot = frame[frame.price_low > 0].sort_values("price_low").reset_index(drop=True)
    lo, hi = plot.price_low.to_numpy(float), plot.price_high.to_numpy(float)
    mid = (lo + hi) / 2
    has_cmp = cmp_frame is not None
    fig = make_subplots(rows=2 if has_cmp else 1, cols=1, shared_xaxes=True, vertical_spacing=0.04,
                        row_heights=[0.72, 0.28] if has_cmp else [1.0])
    delta = None
    if has_cmp:
        cp = cmp_frame[cmp_frame.price_low > 0]
        fig.add_bar(x=(cp.price_low + cp.price_high) / 2, y=cp.btc_amount, width=(cp.price_high - cp.price_low) * 0.92,
                    name=cmp_label, marker_color="rgba(148,163,184,.28)",
                    marker_line=dict(color="rgba(148,163,184,.7)", width=1),
                    hovertemplate=f"{cmp_label}<br>BTC: %{{y:,.0f}}<extra></extra>", row=1, col=1)
        bd = bucket_deltas(plot, cmp_frame)
        old = pd.Series(bd.old.values, index=bd.low.values).reindex(lo).to_numpy(float) if not bd.empty else np.full(len(lo), np.nan)
        delta = plot.btc_amount.to_numpy(float) - old
        custom = np.column_stack([lo, hi, old, delta])
        hover = (f"{label}<br>$%{{customdata[0]:,.0f}} – $%{{customdata[1]:,.0f}}<br>BTC: %{{y:,.0f}}<br>"
                 f"{cmp_label}: %{{customdata[2]:,.0f}}<br>Thay đổi: %{{customdata[3]:+,.0f}} BTC<extra></extra>")
    else:
        custom = np.column_stack([lo, hi])
        hover = f"{label}<br>$%{{customdata[0]:,.0f}} – $%{{customdata[1]:,.0f}}<br>BTC: %{{y:,.0f}}<extra></extra>"
    fig.add_bar(x=mid, y=plot.btc_amount, width=(hi - lo) * (0.72 if has_cmp else 0.92), name=label,
                marker_color=np.where(mid < price, GREEN, RED), customdata=custom, hovertemplate=hover, row=1, col=1)
    if has_cmp:
        fig.add_bar(x=mid, y=delta, width=(hi - lo) * 0.85, name="Δ toàn bộ", showlegend=False,
                    marker_color=np.where(np.nan_to_num(delta) >= 0, GREEN, RED),
                    hovertemplate="$%{x:,.0f}<br>Δ toàn bộ: %{y:+,.0f} BTC<extra></extra>", row=2, col=1)
        if lth_delta is not None:
            ld = pd.Series(lth_delta).reindex(lo).to_numpy(float)
            fig.add_scatter(x=mid, y=ld, mode="markers", name="Δ holder dài hạn (LTH)",
                            marker=dict(color=CYAN, size=6, symbol="diamond"),
                            hovertemplate="$%{x:,.0f}<br>Δ LTH: %{y:+,.0f} BTC<extra></extra>", row=2, col=1)
        fig.add_hline(y=0, line_color=GREY, line_width=1, row=2, col=1)
    for z in supports:
        fig.add_vrect(x0=z["low"], x1=z["high"], fillcolor=GREEN, opacity=0.10, line_width=0, layer="below", row=1, col=1)
    for z in resistances:
        fig.add_vrect(x0=z["low"], x1=z["high"], fillcolor=RED, opacity=0.10, line_width=0, layer="below", row=1, col=1)
    fig.add_vline(x=price, line_dash="dash", line_color="white",
                  annotation_text=f"Giá {fmt_usd(price)}", annotation_position="top left", row=1, col=1)
    if sth:
        fig.add_vline(x=sth, line_dash="dot", line_color=AMBER, annotation_text=f"STH {fmt_usd(sth)}",
                      annotation_position="top right", annotation_y=0.93, annotation_font_color=AMBER, row=1, col=1)
    if lth:
        fig.add_vline(x=lth, line_dash="dot", line_color=CYAN, annotation_text=f"LTH {fmt_usd(lth)}",
                      annotation_position="top right", annotation_y=0.86, annotation_font_color=CYAN, row=1, col=1)
    if has_cmp:
        fig.add_vline(x=price, line_dash="dash", line_color="white", row=2, col=1)
    style_fig(fig, 680 if has_cmp else 560, hovermode="closest")
    xr = [0, max(ath, price) * 1.05]
    fig.update_layout(barmode="overlay", bargap=0.02, showlegend=has_cmp)
    fig.update_xaxes(range=xr, tickformat="$,.0f")
    fig.update_xaxes(title_text="Giá vốn on-chain (USD)", row=2 if has_cmp else 1, col=1)
    fig.update_yaxes(title_text="BTC", row=1, col=1)
    if has_cmp:
        fig.update_yaxes(title_text="Δ BTC", row=2, col=1)
    return fig


def lth_delta_series(store, sel, cmp_date):
    """Δ BTC của holder dài hạn theo bucket (index = price_low), nếu có URPD LTH cho cả 2 ngày."""
    a, b = store.get("frames_lth", {}).get(sel), store.get("frames_lth", {}).get(cmp_date)
    if a is None or b is None:
        return None
    bd = bucket_deltas(a, b)
    return pd.Series(bd.delta.values, index=bd.low.values) if not bd.empty else None


def bucket_change_table(frame, cmp_frame, lth_delta, only_big, price):
    bd = bucket_deltas(frame, cmp_frame)
    if bd.empty:
        return None
    bd = bd.sort_values("low", ascending=False)
    if lth_delta is not None:
        bd["lth"] = lth_delta.reindex(bd.low).to_numpy(float)
    if only_big:
        bd = bd[bd.delta.abs() >= 1000]
    out = pd.DataFrame({
        "Vùng giá": [("🟢 " if h <= price else "🔴 ") + f"${a / 1000:,.0f}k–{h / 1000:,.0f}k" for a, h in zip(bd.low, bd.high)],
        "BTC": bd.current.round(0),
        "Δ BTC": bd.delta.round(0),
    })
    if "lth" in bd.columns:
        out["Δ LTH"] = bd.lth.round(0)
    return out


def _color_delta(v):
    if pd.isna(v) or v == 0:
        return ""
    return "color: #34d399; font-weight: 600" if v > 0 else "color: #f87171; font-weight: 600"


def render_flow_analysis(store, sel, cmp_date, frame, cmp_frame, price, lth_delta):
    """BTC rời vùng giá đã đi đâu? — dựa trên cơ chế của URPD."""
    st.subheader(f"🔀 BTC rời các vùng giá đã đi đâu? ({cmp_date} → {sel})")
    d0, d1 = parse_day(cmp_date), parse_day(sel)
    closes = [store["prices"][k] for k in store["dates"] if d0 <= parse_day(k) <= d1 and store["prices"].get(k)]
    closes = closes or [price]
    zone_lo = np.floor(min(closes) * 0.98 / 1000) * 1000
    zone_hi = np.ceil(max(closes) * 1.02 / 1000) * 1000
    avg_px = float(np.mean(closes[1:] if len(closes) > 1 else closes))
    bd = bucket_deltas(frame, cmp_frame)
    if bd.empty:
        st.info("Không đủ dữ liệu để phân tích.")
        return
    bd["mid"] = (bd.low + bd.high) / 2
    in_zone = (bd.high > zone_lo) & (bd.low < zone_hi)
    dep = bd[~in_zone & (bd.delta <= -50)].copy()
    arrivals = float(bd[in_zone].delta.sum())
    if lth_delta is not None:
        dep["lth"] = lth_delta.reindex(dep.low).to_numpy(float)
    dep["kind"] = np.where(dep.mid < zone_lo, "profit", "loss")
    dep["usd"] = -dep.delta * (avg_px - dep.mid)
    prof, loss = dep[dep.kind == "profit"], dep[dep.kind == "loss"]
    moved = float(-dep.delta.sum())
    days = max(1, (d1 - d0).days)
    mined = 450.0 * days

    m = st.columns(4)
    m[0].metric("BTC rời các vùng giá cũ", fmt_btc(moved), help="Tổng các vùng giảm, không tính vùng giá đang giao dịch.")
    m[1].metric(f"Vùng giá giao dịch ${zone_lo / 1000:,.0f}k–{zone_hi / 1000:,.0f}k", fmt_btc(arrivals, signed=True),
                f"gồm ≈ {fmt_btc(mined)} mới đào", delta_color="off",
                help="BTC di chuyển xuất hiện lại ở đây — giá vốn mới = giá lúc di chuyển.")
    m[2].metric("Từ vùng đang lãi (chốt lời?)", fmt_btc(-prof.delta.sum()),
                f"lãi ước tính {fmt_big_usd(prof.usd.sum())}", delta_color="off")
    m[3].metric("Từ vùng đang lỗ (cắt lỗ?)", fmt_btc(-loss.delta.sum()),
                f"lỗ ước tính {fmt_big_usd(-loss.usd.sum())}", delta_color="off")

    if not dep.empty:
        dep["band"] = (dep.low // 5000 * 5000).astype(int)
        grp = dep.groupby(["band", "kind"]).agg(btc=("delta", lambda x: -x.sum())).reset_index()
        grp = grp.sort_values("btc", ascending=False).head(10)
        labels = [f"${b / 1000:,.0f}k–{(b + 5000) / 1000:,.0f}k" for b in grp.band] + \
                 [f"Vùng giá giao dịch ${zone_lo / 1000:,.0f}k–{zone_hi / 1000:,.0f}k"]
        target = len(grp)
        fig = go.Figure(go.Sankey(
            node=dict(label=labels, pad=14, thickness=16,
                      color=[GREEN if k == "profit" else RED for k in grp.kind] + ["#f8fafc"]),
            link=dict(source=list(range(len(grp))), target=[target] * len(grp), value=grp.btc.tolist(),
                      color=["rgba(16,185,129,.35)" if k == "profit" else "rgba(239,68,68,.35)" for k in grp.kind],
                      hovertemplate="%{source.label} → %{target.label}<br>%{value:,.0f} BTC<extra></extra>")))
        fig.update_layout(height=360, paper_bgcolor=CHART_BG, font=dict(color="#e5e7eb", size=12),
                          margin=dict(l=10, r=10, t=10, b=10))
        show_fig(fig, "urpd_sankey")

        top = dep.sort_values("delta").head(12)
        rows = pd.DataFrame({
            "Vùng rời đi": [f"${a / 1000:,.0f}k–{h / 1000:,.0f}k" for a, h in zip(top.low, top.high)],
            "Δ BTC": top.delta.round(0),
            "Trong đó LTH": top["lth"].round(0) if "lth" in top.columns else np.nan,
            "Đi đến": f"${zone_lo / 1000:,.0f}k–{zone_hi / 1000:,.0f}k",
            "Trạng thái khi di chuyển": np.where(top.kind == "profit", "🟢 đang lãi", "🔴 đang lỗ"),
            "Lãi/lỗ ước tính": top.usd.map(lambda v: fmt_big_usd(v, signed=True)),
        })
        if "lth" not in top.columns:
            rows = rows.drop(columns=["Trong đó LTH"])
        dcols = [c for c in ("Δ BTC", "Trong đó LTH") if c in rows.columns]
        st.dataframe(rows.style.map(_color_delta, subset=dcols).format({c: "{:+,.0f}" for c in dcols}, na_rep="—"),
                     hide_index=True, **STRETCH)

    lth_spent = float(-np.clip(dep["lth"].fillna(0), None, 0).sum()) if "lth" in dep.columns else None
    lines = [f"Khoảng **{fmt_btc(moved)}** đã rời các vùng giá cũ. Theo cơ chế URPD, BTC nào di chuyển on-chain sẽ "
             f"được ghi giá vốn mới bằng giá lúc di chuyển, nên toàn bộ lượng này **xuất hiện lại ở vùng "
             f"${zone_lo / 1000:,.0f}k–{zone_hi / 1000:,.0f}k** (vùng này tăng {fmt_btc(arrivals, signed=True)}, gồm cả ≈{fmt_btc(mined)} mới đào)."]
    if len(prof):
        lines.append(f"🟢 **{fmt_btc(-prof.delta.sum())}** đến từ vùng đang lãi — nhiều khả năng **chốt lời**, "
                     f"lãi ước tính **{fmt_big_usd(prof.usd.sum())}**.")
    if len(loss):
        lines.append(f"🔴 **{fmt_btc(-loss.delta.sum())}** đến từ vùng đang lỗ — có thể **cắt lỗ/bán hòa vốn**, "
                     f"lỗ ước tính **{fmt_big_usd(-loss.usd.sum())}**.")
    if lth_spent is not None:
        lines.append(f"🧓 Trong đó ít nhất **{fmt_btc(lth_spent)}** là của **holder dài hạn (≥155 ngày)** — nhóm này bán "
                     "thường là tín hiệu phân phối mạnh hơn holder ngắn hạn.")
    elif "frames_lth" in store:
        lines.append("🧓 Phần tách holder dài hạn/ngắn hạn sẽ có sau lần chạy GitHub Actions tới (đang bắt đầu lưu URPD của LTH).")
    real = load_realized()
    if not real.empty:
        rr = real[(real.date > d0) & (real.date <= d1)]
        if not rr.empty:
            lines.append(f"🔁 Đối chiếu BGeometrics cùng kỳ: chốt lời thực tế {fmt_big_usd(rr.realized_profit.sum())}, "
                         f"cắt lỗ thực tế {fmt_big_usd(rr.realized_loss.sum())} (số thực tế lớn hơn vì gồm cả coin "
                         "di chuyển trong vùng giá giao dịch).")
    mk("\n".join(f"- {x}" for x in lines))

    with st.expander("❓ Có biết được BTC đó lên sàn bán hay chỉ chuyển ví không?"):
        mk("\n".join([
            "- **URPD chỉ cho biết coin đã di chuyển và giá vốn cũ của nó**, không cho biết ví đích là sàn hay ví cá nhân. "
            "Coin có thể: nạp lên sàn để bán, chuyển sang ví lạnh/ví mới, gộp UTXO, hoặc sàn tự xoay ví nội bộ.",
            "- **Dấu hiệu nghiêng về bán thật:** lượng lớn rời vùng đang lãi xa (chốt lời) hoặc ngay trên giá (bán hòa vốn), "
            "kèm *Realized P/L* tăng mạnh cùng ngày (trang On-chain) và giá giảm.",
            "- **Dấu hiệu chỉ là chuyển ví:** coin rời đi nhưng Realized P/L thấp, giá không phản ứng, "
            "hoặc lượng lớn ở các vùng rất cũ (dưới $20k) — thường là sàn/tổ chức xoay ví lưu ký.",
            "- **Biết chính xác nạp sàn** cần dữ liệu dòng tiền sàn (Exchange Netflow) — hiện chỉ có ở gói trả phí "
            "(CryptoQuant, Glassnode, BGeometrics Advanced).",
            "- Lãi/lỗ ước tính = lượng BTC × (giá trung bình kỳ − giá giữa vùng), chỉ là xấp xỉ.",
        ]))


def page_urpd():
    ath = ath_value()
    store = get_urpd_store()
    onchain = load_onchain()
    frame_cur, cur_date, cur_price, _ = current_snapshot(store)
    st.title("📊 Phân bố nguồn cung theo giá vốn (URPD)")
    if frame_cur is None:
        st.error("Chưa có dữ liệu URPD.")
        return

    options = list(reversed(store["dates"]))
    if cur_date not in options:
        options.insert(0, cur_date)
    c1, c2 = st.columns(2)
    sel = c1.selectbox("Ngày snapshot", options, index=0)
    cmp_opts = {"Không so sánh": 0, "1 ngày trước": 1, "3 ngày trước": 3, "7 ngày trước": 7, "30 ngày trước": 30}
    cmp_label = c2.selectbox("So sánh với", list(cmp_opts), index=1)

    if sel == cur_date:
        frame, price = frame_cur, cur_price or live_price()
    else:
        frame, price = store["frames"][sel], store["prices"].get(sel) or live_price()
    if not price:
        st.error("Không có giá cho snapshot này.")
        return
    day = parse_day(sel) if is_day_key(sel) else None
    cmp_frame, cmp_date = (None, None)
    if cmp_opts[cmp_label] and day:
        cmp_frame, cmp_date = frame_on(store, sel, cmp_opts[cmp_label])
        if cmp_frame is None:
            cap(f"Chưa có snapshot {cmp_date} để so sánh.")

    sth, _ = latest_value(onchain, "sth_realized_price", on_or_before=day)
    lth, _ = latest_value(onchain, "lth_realized_price", on_or_before=day)
    supports, resistances = find_sr_zones(frame, price)
    lth_d = lth_delta_series(store, sel, cmp_date) if cmp_frame is not None else None
    fig = urpd_figure(frame, price, f"Snapshot {sel}", cmp_frame, f"Snapshot {cmp_date}", sth, lth,
                      supports, resistances, ath, lth_delta=lth_d)
    if cmp_frame is not None:
        left, right = st.columns([2.5, 1.5])
        with left:
            show_fig(fig, key="urpd_main")
        with right:
            only_big = st.toggle("Chỉ vùng thay đổi ≥ 1.000 BTC", value=True, key="urpd_only_big")
            tbl = bucket_change_table(frame, cmp_frame, lth_d, only_big, price)
            if tbl is None or tbl.empty:
                cap("Không có vùng nào thay đổi đáng kể.")
            else:
                cols = [c for c in ("Δ BTC", "Δ LTH") if c in tbl.columns]
                st.dataframe(tbl.style.map(_color_delta, subset=cols).format(
                    {"BTC": "{:,.0f}", **{c: "{:+,.0f}" for c in cols}}, na_rep="—"),
                    hide_index=True, height=640, **STRETCH)
        cap("Hàng dưới biểu đồ = Δ BTC từng cột so với snapshot so sánh (♦ xanh = phần của holder dài hạn). "
            "Bảng bên phải liệt kê đúng các cột đó theo giá từ cao xuống thấp.")
    else:
        show_fig(fig, key="urpd_main")
    cap("🟢 giá vốn dưới giá snapshot · 🔴 trên giá · nền xanh/đỏ = vùng hỗ trợ/kháng cự tự động · "
        "chấm cam/xanh = giá vốn STH/LTH.")
    if cmp_frame is not None:
        render_flow_analysis(store, sel, cmp_date, frame, cmp_frame, price, lth_d)

    # ---- Bảng biến động gộp (cùng mức giá tham chiếu)
    st.subheader("📋 Biến động theo khung thời gian")
    base = snapshot_metrics(frame, price, ath)
    labels = {"below": "Cung dưới giá", "overhead": "Cung giá → ATH", "support_near": "Hỗ trợ (−10% → giá)",
              "resistance_near": "Cản (giá → +10%)", "total": "Tổng URPD"}
    table = {k: {"Chỉ số": v, "Ngày đang xem": fmt_btc(base[k])} for k, v in labels.items()}
    price_row = {"Chỉ số": "Giá đóng cửa", "Ngày đang xem": fmt_usd(price)}
    for n in (1, 3, 7, 30):
        old_frame, old_date = frame_on(store, sel, n) if day else (None, None)
        col = f"Δ {n}D"
        if old_frame is None:
            for k in labels:
                table[k][col] = "—"
            price_row[col] = "—"
            continue
        om = snapshot_metrics(old_frame, price, ath)
        for k in labels:
            table[k][col] = fmt_btc(base[k] - om[k], signed=True)
        op = store["prices"].get(old_date)
        price_row[col] = f"{(price / op - 1) * 100:+.2f}%" if op else "—"
    show_df(pd.DataFrame([price_row] + list(table.values())))
    cap("Mọi Δ tính tại CÙNG mức giá của ngày đang xem cho cả hai snapshot, nên phản ánh thay đổi cấu trúc "
               "giá vốn chứ không bị nhiễu bởi việc giá di chuyển. '—' = history chưa có ngày đó.")

    # ---- Tải toàn bộ thay đổi theo bucket (bảng chi tiết đã nằm cạnh biểu đồ)
    if cmp_frame is not None:
        bd = bucket_deltas(frame, cmp_frame)
        if not bd.empty:
            st.download_button("⬇️ Tải CSV thay đổi toàn bộ bucket", bd.to_csv(index=False).encode("utf-8-sig"),
                               file_name=f"urpd_delta_{cmp_date}_{sel}.csv", mime="text/csv")

    with st.expander("🧠 Cách đọc URPD"):
        mk(
            "- **URPD** cho biết mỗi BTC đang nằm ở mức giá vốn nào (giá lúc nó di chuyển lần cuối).\n"
            "- **Bucket tăng/giảm** là thay đổi phân bố giữa hai snapshot — *không* phải truy vết từng coin, "
            "không tự chứng minh mua/bán, tích lũy hay phân phối.\n"
            "- Cung **trên giá** ≈ lượng BTC đang lỗ, dễ bị bán khi giá hồi về hòa vốn; cung **dưới giá** là nền đỡ tiềm năng.\n"
            "- Đọc cùng giá vốn STH/LTH, Realized P/L và phản ứng giá thay vì một bucket đơn lẻ.")
    with st.expander(f"Xem URPD gốc ({sel})"):
        show_df(frame)


# =============================================================================
# TRANG 3 — ON-CHAIN
# =============================================================================
def _period_filter(df, days):
    if df is None or df.empty:
        return df
    return df[df["date"] >= df["date"].max() - timedelta(days=days - 1)]


def price_series(realized, store):
    """Giá đóng cửa theo ngày: BGeometrics btc-price, bổ sung bằng giá snapshot URPD."""
    s = {}
    for d, p in store["prices"].items():
        s[parse_day(d)] = p
    if realized is not None and not realized.empty:
        for d, p in zip(realized["date"], realized["btc_price"]):
            if safe_float(p):
                s[d] = float(p)
    return pd.DataFrame(sorted(s.items()), columns=["date", "price"])


def page_onchain():
    st.title("⛓️ Chỉ số on-chain")
    realized, onchain, store = load_realized(), load_onchain(), get_urpd_store()
    days = st.radio("Khoảng thời gian", [90, 180, 365], index=1, horizontal=True, format_func=lambda x: f"{x} ngày")
    prices = _period_filter(price_series(realized, store), days)

    # ---- Realized P/L
    st.subheader("💰 Realized Profit / Loss — chốt lời & cắt lỗ thực tế (USD/ngày)")
    rp = _period_filter(realized, days)
    if rp is None or rp.empty:
        st.info("Chưa có dữ liệu Realized P/L (BGeometrics). Chạy workflow Update BTC data.")
    else:
        rp = rp.copy()
        rp["net"] = rp.realized_profit - rp.realized_loss
        rp["net_ema7"] = rp["net"].ewm(span=7, adjust=False).mean()
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        w = 0.78 * 86400000
        fig.add_bar(x=rp.date, y=rp.realized_profit, name="Realized Profit", marker_color="#22c55e", width=w,
                    hovertemplate="%{x|%d/%m/%Y}<br>Profit: $%{y:,.0f}<extra></extra>", secondary_y=False)
        fig.add_bar(x=rp.date, y=-rp.realized_loss, name="Realized Loss", marker_color=RED, width=w,
                    customdata=rp.realized_loss, hovertemplate="%{x|%d/%m/%Y}<br>Loss: $%{customdata:,.0f}<extra></extra>",
                    secondary_y=False)
        fig.add_scatter(x=rp.date, y=rp.net_ema7, name="Net P/L (EMA 7)", mode="lines",
                        line=dict(color="#facc15", width=2.2), secondary_y=False)
        if not prices.empty:
            fig.add_scatter(x=prices.date, y=prices.price, name="Giá BTC", mode="lines",
                            line=dict(color="#f8fafc", width=2), secondary_y=True)
        style_fig(fig, 520)
        fig.update_layout(barmode="relative")
        fig.update_yaxes(title_text="USD/ngày", tickprefix="$", secondary_y=False)
        fig.update_yaxes(title_text="Giá BTC", tickprefix="$", showgrid=False, secondary_y=True)
        show_fig(fig, "realized_pl")
        last = rp.iloc[-1]
        m = st.columns(3)
        m[0].metric(f"Chốt lời {last.date:%d/%m}", fmt_big_usd(last.realized_profit))
        m[1].metric(f"Cắt lỗ {last.date:%d/%m}", fmt_big_usd(last.realized_loss))
        m[2].metric("Ròng", fmt_big_usd(last.net, signed=True))

    # ---- Giá vốn STH / LTH
    st.subheader("🧭 Giá vốn holder ngắn hạn (STH) / dài hạn (LTH)")
    ob = _period_filter(onchain, days)
    has_cb = ob is not None and not ob.empty and any(c in ob.columns for c in ("sth_realized_price", "lth_realized_price"))
    if not has_cb:
        st.info("Chưa có dữ liệu giá vốn STH/LTH.")
    else:
        fig = go.Figure()
        if not prices.empty:
            fig.add_scatter(x=prices.date, y=prices.price, name="Giá BTC", line=dict(color="#f8fafc", width=2))
        for col, name, color in (("sth_realized_price", "Giá vốn STH (< 155 ngày)", AMBER),
                                 ("lth_realized_price", "Giá vốn LTH (≥ 155 ngày)", CYAN)):
            if col in ob.columns and ob[col].notna().any():
                fig.add_scatter(x=ob.date, y=ob[col], name=name, line=dict(color=color, width=2))
        style_fig(fig, 440)
        fig.update_yaxes(tickprefix="$", tickformat=",.0f")
        show_fig(fig, "cost_basis")
        cap("Giá trên đường STH: holder mới đang lãi (pha khỏe). Giá dưới STH: holder mới lỗ, "
                   "các nhịp hồi về STH thường gặp áp lực bán hòa vốn.")

    # ---- % cung đang lỗ
    st.subheader("📉 % nguồn cung đang lỗ")
    if ob is None or ob.empty or "supply_in_loss_percent" not in ob.columns or ob.supply_in_loss_percent.notna().sum() < 2:
        st.info("Chưa đủ lịch sử Supply in Loss (ResearchBitcoin) — dữ liệu sẽ dày dần sau mỗi ngày chạy Actions.")
    else:
        sl = ob.dropna(subset=["supply_in_loss_percent"])
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_scatter(x=sl.date, y=sl.supply_in_loss_percent, name="% cung đang lỗ", fill="tozeroy",
                        line=dict(color=RED, width=2), fillcolor="rgba(239,68,68,.15)", secondary_y=False)
        if not prices.empty:
            fig.add_scatter(x=prices.date, y=prices.price, name="Giá BTC", line=dict(color="#f8fafc", width=1.6),
                            secondary_y=True)
        style_fig(fig, 400)
        fig.update_yaxes(title_text="% cung", ticksuffix="%", secondary_y=False)
        fig.update_yaxes(title_text="Giá BTC", tickprefix="$", showgrid=False, secondary_y=True)
        show_fig(fig, "supply_loss")

    # ---- MVRV / NUPL / SOPR
    st.subheader("📐 Định giá on-chain: MVRV · NUPL · SOPR")
    cols = [c for c in ("mvrv", "nupl", "sopr") if ob is not None and c in ob.columns and ob[c].notna().any()]
    if not cols:
        st.info("Chưa có MVRV/NUPL/SOPR (BGeometrics).")
    else:
        fig = make_subplots(rows=len(cols), cols=1, shared_xaxes=True, vertical_spacing=0.06,
                            subplot_titles=[c.upper() for c in cols])
        ref = {"mvrv": 1.0, "nupl": 0.0, "sopr": 1.0}
        for i, ccol in enumerate(cols, start=1):
            d = ob.dropna(subset=[ccol])
            series = d[ccol].rolling(7, min_periods=1).mean() if ccol == "sopr" else d[ccol]
            fig.add_scatter(x=d.date, y=series, name=ccol.upper() + (" (TB 7 ngày)" if ccol == "sopr" else ""),
                            line=dict(width=2), row=i, col=1)
            fig.add_hline(y=ref[ccol], line_dash="dot", line_color=GREY, row=i, col=1)
        style_fig(fig, 220 * len(cols) + 60)
        fig.update_layout(showlegend=False)
        show_fig(fig, "valuation")
        cap("MVRV < 1 hoặc NUPL < 0: giá thấp hơn giá vốn trung bình toàn thị trường (vùng định giá thấp lịch sử). "
                   "SOPR < 1 kéo dài: thị trường đang bán lỗ. Gói miễn phí BGeometrics trễ ~7 ngày: MVRV/NUPL các ngày "
                   "gần nhất được ước tính từ URPD; SOPR dừng ở ngày nguồn có số.")


# =============================================================================
# TRANG 4 — LỊCH SỬ (heatmap + xu hướng)
# =============================================================================
@st.cache_data(ttl=900, show_spinner="Đang dựng bản đồ nhiệt…")
def heatmap_cached(version, days):
    store = get_urpd_store()
    return build_heatmap_data(store["frames"], store["prices"], days)


@st.cache_data(ttl=900, show_spinner=False)
def zone_series(version, lo, hi):
    """Lượng BTC có giá vốn trong [lo, hi] theo từng ngày + % tổng cung."""
    store = get_urpd_store()
    rows = []
    for d in store["dates"]:
        f = store["frames"][d]
        total = float(f.btc_amount.sum())
        v = btc_in_range(f, lo, hi)
        rows.append({"date": parse_day(d), "btc": v, "pct": v / total * 100 if total else np.nan,
                     "price": store["prices"].get(d, np.nan)})
    df = pd.DataFrame(rows)
    if not df.empty:
        df["chg"] = df.btc.diff()
    return df


def _zone_default():
    """Vùng mặc định: tham số trên URL (?zlo=&zhi=) → secrets WATCH_ZONE_LOW/HIGH → WATCH_ZONE_DEFAULT."""
    lo, hi = WATCH_ZONE_DEFAULT
    lo = safe_float(_secret("WATCH_ZONE_LOW")) or lo
    hi = safe_float(_secret("WATCH_ZONE_HIGH")) or hi
    try:
        qp = st.query_params
        lo = safe_float(qp.get("zlo")) or lo
        hi = safe_float(qp.get("zhi")) or hi
    except Exception:
        pass
    return float(lo), float(hi)


def render_watch_zone(store):
    st.subheader("🎯 Vùng theo dõi tùy chọn")
    dlo, dhi = _zone_default()
    c = st.columns([1, 1, 1.4])
    lo = c[0].number_input("Từ giá (USD)", min_value=0.0, value=dlo, step=100.0, format="%.0f", key="zone_lo")
    hi = c[1].number_input("Đến giá (USD)", min_value=0.0, value=dhi, step=100.0, format="%.0f", key="zone_hi")
    if hi <= lo:
        st.warning("Giá 'Đến' phải lớn hơn giá 'Từ'.")
        return
    try:  # ghi vào URL để lần sau mở lại (hoặc lưu bookmark) vẫn giữ vùng đã chọn
        if (lo, hi) != WATCH_ZONE_DEFAULT:
            st.query_params.update({"zlo": f"{lo:.0f}", "zhi": f"{hi:.0f}"})
    except Exception:
        pass
    z = zone_series(store["version"], lo, hi)
    if z.empty:
        st.info("Chưa có lịch sử URPD.")
        return
    peak = z.loc[z.btc.idxmax()]
    ref_opts = ["Đỉnh của vùng trong lịch sử"] + [d.strftime("%Y-%m-%d") for d in reversed(list(z.date))]
    ref_label = c[2].selectbox("Mốc so sánh", ref_opts, index=0, key="zone_ref")
    ref = peak if ref_label == ref_opts[0] else z[z.date == parse_day(ref_label)].iloc[0]

    last = z.iloc[-1]

    def at(days):
        old = z[z.date <= last.date - timedelta(days=days)]
        return old.iloc[-1] if not old.empty else None

    d1, d30 = at(1), at(30)
    m = st.columns(4)
    m[0].metric(_esc(f"Cung trong vùng ${lo:,.0f}–${hi:,.0f}"), fmt_btc(last.btc),
                f"{last.pct:.2f}% tổng cung", delta_color="off")
    for col, old, name in ((m[1], d1, "So với 1 ngày trước"), (m[3], d30, "So với 30 ngày trước")):
        if old is not None:
            col.metric(name, fmt_btc(last.btc - old.btc, signed=True), f"{old.date:%d/%m} · {old.btc:,.0f}",
                       delta_color="off")
        else:
            col.metric(name, "—", "chưa đủ lịch sử", delta_color="off")
    diff = last.btc - ref.btc
    m[2].metric(f"So với mốc {ref.date:%d/%m}", fmt_btc(diff, signed=True),
                f"{diff / ref.btc * 100:+.1f}% · {'đỉnh ' if ref_label == ref_opts[0] else ''}{ref.btc:,.0f} BTC"
                if ref.btc else None, delta_color="normal")

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_scatter(x=z.date, y=z.btc, name=f"BTC có giá vốn trong ${lo:,.0f}–${hi:,.0f}", mode="lines",
                    line=dict(color="#f5b942", width=2.4), fill="tozeroy", fillcolor="rgba(245,185,66,.12)",
                    customdata=np.column_stack([z.price, z.pct, z.chg.fillna(0)]),
                    hovertemplate="%{x|%d/%m/%Y}<br>Giá BTC: $%{customdata[0]:,.0f}<br>Trong vùng: %{y:,.0f} BTC · "
                                  "%{customdata[1]:.2f}%<br>Δ so với hôm trước: %{customdata[2]:+,.0f} BTC<extra></extra>",
                    secondary_y=False)
    fig.add_scatter(x=z.date, y=z.pct, name="% tổng cung (trục phải)", mode="lines",
                    line=dict(color=GREY, width=1.2, dash="dot"), hoverinfo="skip", secondary_y=True)
    fig.add_vline(x=peak.date, line_dash="dot", line_color="#f5b942")
    fig.add_annotation(x=peak.date, y=peak.btc, text=f"đỉnh {peak.date:%d/%m} · {peak.btc:,.0f}", showarrow=True,
                       arrowhead=2, ax=-40, ay=-30, font=dict(color="#f5b942", size=11))
    if ref_label != ref_opts[0]:
        fig.add_vline(x=ref.date, line_dash="dash", line_color=CYAN)
        fig.add_annotation(x=ref.date, y=ref.btc, text=f"mốc {ref.date:%d/%m}", showarrow=True, ax=40, ay=-30,
                           font=dict(color=CYAN, size=11))
    fig.add_annotation(x=last.date, y=last.btc, text=f"{last.date:%d/%m}: {last.btc:,.0f}", showarrow=True,
                       ax=-50, ay=25, font=dict(color="#f8fafc", size=11))
    style_fig(fig, 420)
    span = max(float(z.btc.max() - z.btc.min()), float(z.btc.max()) * 0.02, 1.0)
    fig.update_yaxes(title_text="BTC trong vùng", range=[max(0.0, float(z.btc.min()) - span * 0.25),
                                                         float(z.btc.max()) + span * 0.15], secondary_y=False)
    fig.update_yaxes(title_text="% tổng cung", ticksuffix="%", showgrid=False, secondary_y=True)
    show_fig(fig, "watch_zone")
    spot = live_price()
    where = ("giá hiện tại đang **trong** vùng" if spot and lo <= spot <= hi else
             f"giá hiện tại {'**trên**' if spot and spot > hi else '**dưới**'} vùng" if spot else "")
    cap(f"Đường vàng = lượng BTC có giá vốn trong vùng này mỗi ngày (bucket cắt ngang biên được tính theo tỷ lệ). "
        f"Tăng = coin được mua/di chuyển ở vùng giá này (tích lũy); giảm = coin trong vùng bị tiêu và chuyển sang giá khác. "
        f"{where}. Lịch sử có {len(z)} ngày ({z.date.min():%d/%m/%Y} → {last.date:%d/%m/%Y}); "
        "GitHub Actions bù dần tới đủ 365 ngày. Đổi vùng ở 2 ô trên — vùng đang chọn được lưu trên địa chỉ trang.")


def page_history():
    ath = ath_value()
    store = get_urpd_store()
    st.title("🗓️ Lịch sử nguồn cung")
    if len(store["dates"]) < 2:
        st.info("Cần ít nhất 2 snapshot URPD trong history.")
        return
    render_watch_zone(store)
    st.divider()
    days = st.radio("Khoảng thời gian", [30, 90, 180, 365], index=1, horizontal=True, format_func=lambda x: f"{x} ngày")
    spot = live_price() or store["prices"].get(store["dates"][-1])

    st.subheader("🌡️ Bản đồ nhiệt: lượng BTC theo vùng giá vốn")
    hm = heatmap_cached(store["version"], days)
    if hm is None:
        st.info("Không đủ snapshot trong khoảng thời gian này.")
    else:
        z = hm["z"]
        positive = z[z > 0]
        zmax = max(float(np.percentile(positive, 99)) if positive.size else 1.0, 1.0)
        y_mid = (hm["edges"][:-1] + hm["edges"][1:]) / 2
        labels = [f"${int(a):,}–${int(b):,}" for a, b in zip(hm["edges"][:-1], hm["edges"][1:])]
        custom = np.stack([np.broadcast_to(np.array(labels, dtype=object)[:, None], z.shape), z, hm["delta"]], axis=-1)
        fig = go.Figure(go.Heatmap(
            x=hm["dates"], y=y_mid, z=np.clip(z, 0, zmax), zmin=0, zmax=zmax, customdata=custom,
            colorscale=[[0, "#170b2f"], [0.12, "#35105a"], [0.28, "#4146a5"], [0.45, "#2f83b7"],
                        [0.62, "#28b78e"], [0.8, "#8bd646"], [1, "#fff36b"]],
            colorbar=dict(title="BTC"), hovertemplate="%{x}<br>%{customdata[0]}<br>%{customdata[1]:,.0f} BTC"
                                                      "<br>Δ so với snapshot trước: %{customdata[2]:+,.0f} BTC<extra></extra>"))
        fig.add_scatter(x=hm["dates"], y=hm["prices"], mode="lines", name="Giá BTC",
                        line=dict(color="#ffffff", width=2.2))
        lo_default = max(0, int((spot or 60000) * 0.6 // 1000 * 1000))
        hi_default = int(min(hm["edges"][-1], (spot or 60000) * 1.45) // 1000 * 1000)
        rng = st.slider("Phạm vi giá hiển thị (USD)", 0, int(hm["edges"][-1]), (lo_default, max(hi_default, lo_default + 1000)),
                        step=1000, format="$%d")
        style_fig(fig, 640, hovermode="closest")
        fig.update_layout(yaxis=dict(range=list(rng), tickformat="$,.0f", title="Giá vốn (USD)"),
                          xaxis=dict(type="category", nticks=16, tickangle=-40))
        show_fig(fig, "heatmap")
        cap("Màu = lượng BTC đang có giá vốn trong mỗi vùng $1.000 (thang màu cắt ở phân vị 99%). "
                   "Vệt sáng kéo dài theo thời gian = vùng giá vốn được tích lũy/giữ lâu.")

    st.subheader("📈 Xu hướng cấu trúc nguồn cung")
    dm = daily_metrics_table(store["version"], ath)
    dm = _period_filter(dm, days)
    if dm is None or len(dm) < 2:
        st.info("Chưa đủ dữ liệu.")
        return
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    for col, name, color in (("overhead", "Cung giá → ATH (≈ cung đang lỗ theo URPD)", RED),
                             ("support_near", "Hỗ trợ −10%", GREEN), ("resistance_near", "Cản +10%", AMBER)):
        fig.add_scatter(x=dm.date, y=dm[col], name=name, line=dict(color=color, width=2), secondary_y=False)
    fig.add_scatter(x=dm.date, y=dm.price, name="Giá BTC", line=dict(color="#f8fafc", width=1.6, dash="dot"),
                    secondary_y=True)
    style_fig(fig, 460)
    fig.update_yaxes(title_text="BTC", secondary_y=False)
    fig.update_yaxes(title_text="Giá", tickprefix="$", showgrid=False, secondary_y=True)
    show_fig(fig, "trend")
    first, last = dm.iloc[0], dm.iloc[-1]
    rows = [["Giá đóng cửa", fmt_usd(first.price), fmt_usd(last.price), f"{(last.price / first.price - 1) * 100:+.2f}%"]]
    for col, name in (("below", "Cung dưới giá"), ("overhead", "Cung giá → ATH"), ("support_near", "Hỗ trợ −10%"),
                      ("resistance_near", "Cản +10%"), ("total", "Tổng URPD")):
        rows.append([name, fmt_btc(first[col]), fmt_btc(last[col]), fmt_btc(last[col] - first[col], signed=True)])
    show_df(pd.DataFrame(rows, columns=["Chỉ số", f"{first.date:%d/%m/%Y}", f"{last.date:%d/%m/%Y}", "Thay đổi"]))
    cap("Mỗi điểm tính tại giá đóng cửa của chính ngày đó (khác bảng Δ ở trang URPD dùng cùng một mốc giá).")


# =============================================================================
# TRANG 5 — TIN TỨC
# =============================================================================
def page_news():
    st.title("📰 Tin tức & lịch vĩ mô")
    news = fetch_news_radar()
    events = upcoming_btc_events(7)
    left, right = st.columns([1.4, 1])
    with left:
        cats = [c for c, _ in NEWS_QUERIES]
        chosen = st.multiselect("Chủ đề", cats, default=cats)
        items = [n for n in news if n["category"] in chosen][:30]
        if not items:
            st.info("Chưa lấy được tin (hoặc không có tin cho chủ đề đã chọn).")
        tr = translate_batch(tuple(n["title"] for n in items))
        for n in items:
            impact, direction = news_tag(n["title"], n["category"])
            age_h = max(0.0, (utc_now() - n["published"]).total_seconds() / 3600)
            age = f"{age_h:.0f} giờ trước" if age_h < 24 else f"{age_h / 24:.1f} ngày trước"
            mk(f"**{tr.get(n['title']) or n['title']}**  \n"
                        f"<small><i>{n['title']}</i></small>  \n"
                        f"`{n['source']}` · `{n['category']}` · {direction} · `{age}` · [Đọc tin gốc]({n['link']})",
                        unsafe_allow_html=True)
            st.divider()
        cap("Tiêu đề tiếng Việt là bản dịch máy. Nhãn 🟢/🔴 gắn tự động theo từ khóa, chỉ để lọc nhanh.")
    with right:
        st.subheader("⏰ 7 ngày tới")
        if events:
            cap(f"Nguồn: {events[0]['source']}")
            for e in events:
                mk(f"**{e['date']} · {e['time']}**  \n{e['event']}  \n{e['impact']} · {e['why']}")
                st.divider()
        else:
            st.info("Không có sự kiện vĩ mô Mỹ quan trọng trong 7 ngày tới.")


# =============================================================================
# TRANG — PHÁI SINH & THANH KHOẢN
# =============================================================================
def page_market():
    st.title("📈 Phái sinh · thanh khoản · tâm lý")
    mdf, src, now, upd = load_market()
    status = load_status()
    if mdf.empty:
        st.info("Chưa có dữ liệu. Chạy workflow **Update BTC data** (tab Actions → Run workflow).")
        errs = (status.get("market_extra") or {}).get("errors") or []
        if errs:
            cap("Lỗi lần chạy gần nhất: " + " | ".join(errs))
        return
    upd_vn = (datetime.strptime(upd, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).astimezone(VN_TZ)
              .strftime("%d/%m %H:%M") if upd else "N/A")
    cap(f"Nguồn: OKX (phái sinh, giá), Hyperliquid, DeFiLlama (stablecoin), Coinbase + OKX (Premium), "
        f"alternative.me (Sợ hãi & Tham lam) • cập nhật {upd_vn} giờ VN • file: {src}")
    for e in (status.get("market_extra") or {}).get("errors") or []:
        cap(f"⚠️ {e}")
    days = st.radio("Khoảng thời gian", [30, 90, 180, 365], index=1, horizontal=True,
                    format_func=lambda x: f"{x} ngày", key="mkt_days")
    v = mdf[mdf.date >= mdf.date.max() - timedelta(days=days - 1)].copy()

    # ---- Thẻ số
    c = st.columns(4)
    f, _ = _mval(mdf, "funding_apr")
    c[0].metric("Funding perp BTC (OKX)", f"{f:+.1f}%/năm" if f is not None else "N/A",
                f"Hyperliquid {now['hl_funding_apr']:+.1f}%" if now.get("hl_funding_apr") is not None else None, delta_color="off")
    oi, _ = _mval(mdf, "oi_usd")
    oi7, _ = _mval(mdf, "oi_usd", 7)
    c[1].metric("Open interest (OKX)", fmt_big_usd(oi), f"{(oi / oi7 - 1) * 100:+.1f}% 7D" if oi and oi7 else None,
                delta_color="off")
    ls, _ = _mval(mdf, "long_short_ratio")
    c[2].metric("Long/short tài khoản", f"{ls:.2f}" if ls is not None else "N/A",
                ("nghiêng long" if ls > 1.2 else "nghiêng short" if ls < 0.9 else "cân bằng") if ls is not None else None,
                delta_color="off")
    tk = v.dropna(subset=["taker_buy", "taker_sell"]).tail(7) if {"taker_buy", "taker_sell"}.issubset(v.columns) else pd.DataFrame()
    ratio = float(tk.taker_buy.sum() / tk.taker_sell.sum()) if len(tk) and tk.taker_sell.sum() else None
    c[3].metric("Mua/bán chủ động 7 ngày", f"{ratio:.3f}" if ratio else "N/A",
                ("bên mua áp đảo" if ratio > 1.03 else "bên bán áp đảo" if ratio < 0.97 else "cân bằng") if ratio else None,
                delta_color="off")
    c = st.columns(4)
    s, _ = _mval(mdf, "stables_total")
    s7, _ = _mval(mdf, "stables_total", 7)
    c[0].metric("Tổng cung stablecoin", fmt_big_usd(s), f"{fmt_big_usd(s - s7, signed=True)} 7D" if s and s7 else None)
    u, _ = _mval(mdf, "usdt")
    uc, _ = _mval(mdf, "usdc")
    c[1].metric("USDT · USDC", fmt_big_usd(u), f"USDC {fmt_big_usd(uc)}" if uc else None, delta_color="off")
    pr = premium_7d(mdf)
    c[2].metric("Coinbase Premium TB 7 ngày", f"{pr:+.3f}%" if pr is not None else "N/A",
                f"giờ gần nhất {now['cb_premium_now']:+.3f}%" if now.get("cb_premium_now") is not None else None,
                delta_color="off")
    fg, _ = _mval(mdf, "fng")
    cls = mdf.dropna(subset=["fng"]).fng_class.iloc[-1] if fg is not None and "fng_class" in mdf.columns else ""
    fg7, _ = _mval(mdf, "fng", 7)
    c[3].metric("Sợ hãi & Tham lam", f"{fg:.0f} · {FNG_VI.get(cls, cls)}" if fg is not None else "N/A",
                f"{fg - fg7:+.0f} so với 7 ngày trước" if fg is not None and fg7 is not None else None, delta_color="off")

    # ---- Phái sinh
    st.subheader("⚖️ Phái sinh BTC — funding & open interest")
    if {"funding_apr", "oi_usd"} & set(v.columns):
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.06, row_heights=[0.5, 0.5],
                            specs=[[{"secondary_y": True}], [{"secondary_y": True}]])
        if "funding_apr" in v.columns:
            fv = v.dropna(subset=["funding_apr"])
            fig.add_bar(x=fv.date, y=fv.funding_apr, name="Funding (%/năm)",
                        marker_color=[AMBER if x > 20 else (RED if x < 0 else GREEN) for x in fv.funding_apr], row=1, col=1)
        if "price" in v.columns:
            pv = v.dropna(subset=["price"])
            fig.add_scatter(x=pv.date, y=pv.price, name="Giá BTC", line=dict(color="#f8fafc", width=1.6),
                            row=1, col=1, secondary_y=True)
        if "oi_usd" in v.columns:
            ov = v.dropna(subset=["oi_usd"])
            fig.add_scatter(x=ov.date, y=ov.oi_usd, name="Open interest (USD)", line=dict(color=CYAN, width=2), row=2, col=1)
        if "long_short_ratio" in v.columns:
            lv = v.dropna(subset=["long_short_ratio"])
            fig.add_scatter(x=lv.date, y=lv.long_short_ratio, name="Long/short", line=dict(color="#c084fc", width=1.6),
                            row=2, col=1, secondary_y=True)
        style_fig(fig, 560)
        fig.update_yaxes(title_text="Funding %/năm", row=1, col=1, secondary_y=False)
        fig.update_yaxes(title_text="Giá", tickprefix="$", showgrid=False, row=1, col=1, secondary_y=True)
        fig.update_yaxes(title_text="OI", tickprefix="$", row=2, col=1, secondary_y=False)
        fig.update_yaxes(title_text="Long/short", showgrid=False, row=2, col=1, secondary_y=True)
        show_fig(fig, "mkt_deriv")
        cap("Funding > 20%/năm (vàng) = phe long trả phí cao, đòn bẩy dồn một phía. OI tăng nhanh + funding cao "
            "→ dễ có cú quét thanh lý. OKX công bố OI/long-short theo ngày; lịch sử ngắn sẽ dài dần.")

    # ---- Taker
    tv = v.dropna(subset=["taker_buy", "taker_sell"]) if {"taker_buy", "taker_sell"}.issubset(v.columns) else pd.DataFrame()
    if len(tv):
        r_ = tv.taker_buy / tv.taker_sell
        fig = go.Figure(go.Bar(x=tv.date, y=r_ - 1, base=1, name="Mua/bán chủ động",
                               marker_color=[GREEN if x >= 1 else RED for x in r_],
                               hovertemplate="%{x|%d/%m}<br>%{y:.3f}<extra></extra>"))
        style_fig(fig, 280)
        fig.update_layout(title=dict(text="Tỷ lệ khối lượng mua chủ động / bán chủ động (hợp đồng OKX)", font=dict(size=14)))
        show_fig(fig, "mkt_taker")

    # ---- Stablecoin
    st.subheader("💵 Thanh khoản stablecoin")
    if "stables_total" in v.columns and v.stables_total.notna().any():
        sv = v.dropna(subset=["stables_total"]).copy()
        full = mdf.dropna(subset=["stables_total"]).set_index("date").stables_total
        sv["chg7"] = [full.get(d) - full[full.index <= d - timedelta(days=7)].iloc[-1]
                      if (full.index <= d - timedelta(days=7)).any() else np.nan for d in sv.date]
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_bar(x=sv.date, y=sv.chg7, name="Thay đổi 7 ngày", marker_color=[GREEN if x >= 0 else RED for x in sv.chg7.fillna(0)],
                    secondary_y=False)
        fig.add_scatter(x=sv.date, y=sv.stables_total, name="Tổng cung", line=dict(color=CYAN, width=2), secondary_y=True)
        style_fig(fig, 380)
        fig.update_yaxes(title_text="Δ 7 ngày", tickprefix="$", secondary_y=False)
        fig.update_yaxes(title_text="Tổng cung", tickprefix="$", showgrid=False, secondary_y=True)
        show_fig(fig, "mkt_stables")
        cap("Thanh xanh = stablecoin được phát hành thêm (tiền mới vào crypto). Thường đi trước các nhịp tăng; "
            "giảm kéo dài = tiền rút khỏi thị trường.")
    else:
        cap("Chưa có dữ liệu stablecoin.")

    # ---- Coinbase premium + F&G
    a, b = st.columns(2)
    with a:
        st.subheader("🇺🇸 Coinbase Premium")
        if "cb_premium_pct" in v.columns and v.cb_premium_pct.notna().any():
            pv = v.dropna(subset=["cb_premium_pct"])
            fig = go.Figure(go.Bar(x=pv.date, y=pv.cb_premium_pct, marker_color=[GREEN if x >= 0 else RED for x in pv.cb_premium_pct],
                                   hovertemplate="%{x|%d/%m}<br>%{y:+.3f}%<extra></extra>"))
            style_fig(fig, 320)
            fig.update_yaxes(ticksuffix="%")
            show_fig(fig, "mkt_premium")
            cap("Trung bình theo giờ trong ngày của (giá Coinbase / giá OKX − 1). Dương kéo dài = nhà đầu tư Mỹ mua mạnh.")
        else:
            cap("Chưa có dữ liệu.")
    with b:
        st.subheader("😨 Sợ hãi & Tham lam")
        if "fng" in v.columns and v.fng.notna().any():
            fv = v.dropna(subset=["fng"])
            fig = go.Figure()
            for lo, hi, col in ((0, 25, "rgba(239,68,68,0.15)"), (75, 100, "rgba(16,185,129,0.15)")):
                fig.add_hrect(y0=lo, y1=hi, fillcolor=col, line_width=0)
            fig.add_scatter(x=fv.date, y=fv.fng, mode="lines", line=dict(color=AMBER, width=2), name="Chỉ số",
                            customdata=[FNG_VI.get(x, x) for x in fv.fng_class],
                            hovertemplate="%{x|%d/%m}<br>%{y:.0f} · %{customdata}<extra></extra>")
            style_fig(fig, 320)
            fig.update_yaxes(range=[0, 100])
            show_fig(fig, "mkt_fng")
            cap("Dưới 25 = sợ hãi cực độ (thường gần đáy ngắn hạn), trên 75 = tham lam cực độ (thường gần đỉnh).")
        else:
            cap("Chưa có dữ liệu.")


# =============================================================================
# TRANG — DÒNG TIỀN ETF
# =============================================================================
@st.cache_data(ttl=900, show_spinner=False)
def load_etf():
    data, source = read_data_file(ETF_FILE)
    data = data if isinstance(data, dict) else {}
    alts = {s: (etf_frame(v), v.get("error")) for s, v in (data.get("alts") or {}).items() if isinstance(v, dict)}
    return etf_frame(data), source, data.get("source"), data.get("updated_at_utc"), alts


def _etf_btc_equiv(df, store):
    """Quy đổi USD → BTC theo giá đóng cửa ngày (từ lịch sử URPD) — ước tính."""
    px = pd.Series({parse_day(k): v for k, v in store["prices"].items()}).sort_index()
    if px.empty or df.empty:
        return pd.Series(np.nan, index=df.index)
    idx = pd.to_datetime(pd.Series(px.index))
    p = pd.merge_asof(pd.DataFrame({"d": pd.to_datetime(df.date)}).reset_index(),
                      pd.DataFrame({"d": idx, "p": px.values}), on="d", direction="backward")
    return pd.Series((df.total.values / p.p.values), index=df.index)


def render_etf_cards(stt, btc_last=None):
    c = st.columns(4)
    c[0].metric(f"Phiên {stt['date']:%d/%m}", fmt_big_usd(stt["last"], signed=True),
                f"≈ {btc_last:+,.0f} BTC" if btc_last is not None and np.isfinite(btc_last) else None,
                delta_color="normal" if stt["last"] >= 0 else "inverse")
    c[1].metric("5 phiên gần nhất", fmt_big_usd(stt["sum_5"], signed=True))
    c[2].metric("20 phiên gần nhất", fmt_big_usd(stt["sum_20"], signed=True))
    k = stt["streak"]
    c[3].metric("Chuỗi liên tiếp", f"{abs(k)} phiên {'hút ròng' if k > 0 else 'rút ròng'}")
    if stt.get("aum"):
        cap(f"Tổng tài sản các quỹ: **{fmt_big_usd(stt['aum'])}**"
            + (f" · lũy kế tiền vào từ khi ra mắt: **{fmt_big_usd(stt['cum'], signed=True)}**" if stt.get("cum") is not None else "")
            + f" · 5 phiên = {stt['sum_5'] / stt['aum'] * 100:+.2f}% tổng tài sản.")


def signed_table(df, cols, digits=2):
    """Số có dấu dạng chuỗi (ô trống hiện '—') + tô xanh/đỏ theo dấu."""
    out = df.copy()
    for c in cols:
        out[c] = [("—" if v is None or not np.isfinite(v) else f"{v:+,.{digits}f}") for v in pd.to_numeric(out[c], errors="coerce")]

    def color(v):
        if not isinstance(v, str) or v == "—" or v.strip("+-0.,") == "":
            return ""
        return "color: #34d399; font-weight: 600" if v.startswith("+") else "color: #f87171; font-weight: 600"
    return out.style.map(color, subset=cols)


def etf_compare_table(btc_df, alts):
    rows = []
    for sym, (df, err) in [("BTC", (btc_df, None))] + [(s, alts.get(s, (pd.DataFrame(), "chưa lấy"))) for s in ETF_ALTS]:
        stt = etf_stats(df) if df is not None and not df.empty else None
        label = "₿ Bitcoin" if sym == "BTC" else ETF_ALT_LABEL.get(sym, sym)
        if not stt:
            rows.append({"Coin": label, "Phiên": "—", "Ròng phiên": np.nan, "5 phiên": np.nan, "20 phiên": np.nan,
                         "Chuỗi": err or "chưa có dữ liệu", "Tổng tài sản": "—", "Lũy kế từ đầu": "—"})
            continue
        k = stt["streak"]
        rows.append({"Coin": label, "Phiên": f"{stt['date']:%d/%m}", "Ròng phiên": stt["last"] / 1e6,
                     "5 phiên": stt["sum_5"] / 1e6, "20 phiên": stt["sum_20"] / 1e6,
                     "Chuỗi": f"{abs(k)} phiên {'vào' if k > 0 else 'ra'}",
                     "Tổng tài sản": fmt_big_usd(stt["aum"]) if stt.get("aum") else "—",
                     "Lũy kế từ đầu": fmt_big_usd(stt["cum"], signed=True) if stt.get("cum") is not None else "—"})
    return signed_table(pd.DataFrame(rows), ["Ròng phiên", "5 phiên", "20 phiên"])


def render_etf_asset(df, key, coin="BTC", store=None):
    """Thẻ số + biểu đồ + bảng từng quỹ cho 1 coin."""
    df = df.copy()
    has_btc = coin == "BTC" and store is not None
    df["coin_eq"] = _etf_btc_equiv(df, store) if has_btc else np.nan
    stt = etf_stats(df)
    render_etf_cards(stt, df.coin_eq.iloc[-1] if has_btc else None)
    stale_warning(f"Phiên ETF {coin}", df.date.iloc[-1], 4)

    n = st.radio("Khoảng hiển thị", [30, 60, 120, 0], index=1, horizontal=True,
                 format_func=lambda x: "Tất cả" if x == 0 else f"{x} phiên", key=f"etf_n_{key}")
    v = df.tail(n) if n else df
    v = v.assign(cum_view=v.total.cumsum())
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    if has_btc:
        fig.add_bar(x=v.date, y=v.total, name="Ròng / phiên", marker_color=[GREEN if x >= 0 else RED for x in v.total],
                    customdata=np.stack([v.coin_eq.fillna(0)], axis=-1),
                    hovertemplate="%{x|%d/%m/%Y}<br>%{y:$,.0f}<br>≈ %{customdata[0]:+,.0f} BTC<extra></extra>",
                    secondary_y=False)
    else:
        fig.add_bar(x=v.date, y=v.total, name="Ròng / phiên", marker_color=[GREEN if x >= 0 else RED for x in v.total],
                    hovertemplate="%{x|%d/%m/%Y}<br>%{y:$,.0f}<extra></extra>", secondary_y=False)
    fig.add_scatter(x=v.date, y=v.cum_view, name="Lũy kế trong khoảng", mode="lines",
                    line=dict(color=AMBER, width=2.2), secondary_y=True)
    if has_btc:
        prices = price_series(load_realized(), store)
        if not prices.empty:
            pp = prices[prices.date >= v.date.min()]
            fig.add_scatter(x=pp.date, y=pp.price, name="Giá BTC", mode="lines",
                            line=dict(color="#f8fafc", width=1.4, dash="dot"), secondary_y=True, visible="legendonly")
    style_fig(fig, 430)
    fig.update_yaxes(title_text="USD/phiên", tickprefix="$", secondary_y=False)
    fig.update_yaxes(title_text="Lũy kế (USD)", tickprefix="$", showgrid=False, secondary_y=True)
    show_fig(fig, f"etf_flows_{key}")
    cap(f"Thanh xanh = tiền vào ETF (quỹ phải mua {coin} thật), đỏ = tiền rút (quỹ bán {coin}).")

    fcols = [c for c in etf_fund_cols(df) if df[c].fillna(0).abs().sum() > 0]
    if not fcols:
        cap("Chưa có số liệu từng quỹ cho coin này (chỉ có tổng).")
        return
    a, b = st.columns([1, 1.4])
    with a:
        tot = v[fcols].sum(min_count=1).dropna().sort_values()
        tot.index = [c[2:] for c in tot.index]
        fig2 = go.Figure(go.Bar(x=tot.values, y=[f"{t} · {ETF_NAMES.get(t, '')}" for t in tot.index], orientation="h",
                                marker_color=[GREEN if x >= 0 else RED for x in tot.values],
                                hovertemplate="%{y}<br>%{x:$,.0f}<extra></extra>"))
        style_fig(fig2, 90 + 28 * len(tot), hovermode="closest")
        fig2.update_layout(title=dict(text=f"Theo quỹ — {'toàn bộ' if not n else f'{n} phiên'}", font=dict(size=14)))
        fig2.update_xaxes(tickprefix="$")
        show_fig(fig2, f"etf_funds_{key}")
    with b:
        tail = df.tail(10).iloc[::-1]
        tbl = pd.DataFrame({"Ngày": tail.date.map(lambda d: f"{d:%d/%m}"), "Tổng": tail.total.values / 1e6})
        for c in fcols:
            tbl[c[2:]] = tail[c].values / 1e6
        num = [c for c in tbl.columns if c != "Ngày"]
        mk("**10 phiên gần nhất (triệu USD)**")
        st.dataframe(signed_table(tbl, num), hide_index=True, **STRETCH)
        if tbl[num[1:]].iloc[0].isna().all() and len(num) > 1:
            cap("Phiên mới nhất: SoSoValue chưa có số từng quỹ (thường bổ sung sau 1 phiên).")


def page_etf():
    st.title("🏦 Dòng tiền ETF giao ngay (Mỹ)")
    df, file_src, data_src, upd, alts = load_etf()
    store = get_urpd_store()
    status = load_status()
    if df.empty:
        st.info("Chưa có dữ liệu ETF. Chạy workflow **Update BTC data** (tab Actions → Run workflow). "
                "Nếu vẫn trống: thêm secret `SOSOVALUE_API_KEY` (đăng ký miễn phí tại sosovalue.com/developer) "
                "hoặc `ETF_FLOWS_API_KEY` (yasinozen.com/etf-flows).")
        errs = (status.get("etf") or {}).get("errors") or []
        if errs:
            cap("Lỗi lần chạy gần nhất: " + " | ".join(redact(e)[:160] for e in errs))
        return
    upd_vn = ""
    if upd:
        upd_vn = datetime.strptime(upd, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).astimezone(VN_TZ).strftime("%d/%m %H:%M")
    cap(f"Nguồn số liệu: {data_src or 'N/A'} • cập nhật {upd_vn} giờ VN • file: {file_src}. "
        "Quỹ công bố sau giờ đóng cửa Mỹ → số của phiên hôm qua thường đủ vào sáng hôm sau (giờ VN). "
        f"Lấy lại tối đa mỗi {ETF_MIN_INTERVAL_H} giờ để tiết kiệm lượt gọi SoSoValue.")

    st.subheader("📋 So sánh các coin (triệu USD)")
    st.dataframe(etf_compare_table(df, alts), hide_index=True, **STRETCH)
    cap("ETF altcoin còn nhỏ: vài triệu USD/phiên đã là đáng kể. So với quy mô quỹ (Tổng tài sản) để thấy mức độ.")
    if not alts:
        cap("Chưa có dữ liệu ETF altcoin — cần secret `SOSOVALUE_API_KEY` và chạy lại workflow.")

    tabs = st.tabs(["₿ Bitcoin"] + [ETF_ALT_LABEL.get(s, s) for s in ETF_ALTS])
    with tabs[0]:
        render_etf_asset(df, "btc", "BTC", store)
    for tab, sym in zip(tabs[1:], ETF_ALTS):
        with tab:
            adf, err = alts.get(sym, (pd.DataFrame(), None))
            if adf is None or adf.empty:
                st.info(f"Chưa có dữ liệu ETF {sym}." + (f" Lỗi: {err}" if err else ""))
                continue
            if err:
                cap(f"⚠️ Lần lấy gần nhất lỗi ({err}) — đang hiển thị dữ liệu đã lưu.")
            render_etf_asset(adf, sym.lower(), sym)

    with st.expander("📖 Cách đọc dòng tiền ETF"):
        mk("- ETF giao ngay phải **mua coin thật** khi có tiền vào và **bán coin** khi bị rút → đây là cầu/cung trực tiếp "
           "từ nhà đầu tư tổ chức Mỹ.\n"
           "- Một phiên lẻ ít ý nghĩa; nhìn **tổng 5–20 phiên** và **chuỗi** hút/rút liên tiếp.\n"
           "- Rút ròng kéo dài + URPD cho thấy cắt lỗ ở vùng giá cao → áp lực bán đến từ cả tổ chức và holder ngắn hạn.\n"
           "- Hút ròng trong lúc giá giảm = có người mua đỡ giá ở vùng đó.\n"
           "- BTC: chú ý **IBIT** (lớn nhất) và **FBTC**; GBTC phí cao nên thường bị rút đều.\n"
           "- NEAR: quỹ chính là **NRR** (Bitwise). LINK: **GLNK** (Grayscale) và **CLNK** (Bitwise). "
           "Quỹ altcoin nhỏ nên 1 lệnh lớn có thể làm số nhảy mạnh — xem cùng tỉ lệ so với tổng tài sản.\n"
           "- Quy đổi sang BTC dùng giá đóng cửa ngày, chỉ là ước tính.")


# =============================================================================
# ĐIỂM VÀO
# =============================================================================
BTC_PAGES = [
    (page_overview, "Tổng quan", "📊", "tong-quan"),
    (page_etf, "Dòng tiền ETF", "🏦", "etf"),
    (page_market, "Phái sinh & thanh khoản", "📈", "phai-sinh"),
    (page_urpd, "URPD", "🧱", "urpd"),
    (page_onchain, "On-chain", "⛓️", "on-chain"),
    (page_history, "Lịch sử", "🗓️", "lich-su"),
    (page_news, "Tin tức", "📰", "tin-tuc"),
]


def main_app():
    st.set_page_config(layout="wide", page_title=f"BTC URPD Monitor {APP_VERSION}", page_icon="🪙")
    inject_css()
    if hasattr(st, "navigation") and hasattr(st, "Page"):
        btc = [st.Page(fn, title=title, icon=icon, url_path=path, default=(i == 0))
               for i, (fn, title, icon, path) in enumerate(BTC_PAGES)]
        extra = [st.Page(p) for p in sorted(glob.glob("pages/*.py"))]  # giữ các trang cũ, vd NEAR Monitor
        nav = st.navigation({"Bitcoin": btc, **({"Khác": extra} if extra else {})})
        # Thanh thiết lập đặt ở file chính để giữ giá trị khi chuyển trang; ẩn ở các trang không phải BTC.
        if nav.title in {t for _, t, _, _ in BTC_PAGES}:
            render_sidebar()
        nav.run()
    else:  # Streamlit quá cũ: chọn trang bằng radio
        titles = [t for _, t, _, _ in BTC_PAGES]
        choice = st.sidebar.radio("Trang", titles)
        render_sidebar()
        BTC_PAGES[titles.index(choice)][0]()


if UPDATE_MODE:
    run_update()
else:
    main_app()
