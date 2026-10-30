"""
BTC URPD Monitor V70
====================

Một file, hai chế độ chạy:

  • Dashboard (Streamlit Cloud):   streamlit run app.py
      → CHỈ ĐỌC dữ liệu đã lưu trong repo (urpd_history.json, ...).
        Giá BTC, vốn hóa, tin tức, lịch kinh tế vẫn lấy trực tiếp.

  • Cập nhật dữ liệu (GitHub Actions):   python app.py --update-data
      → Lấy URPD (Bitview), Supply in Loss + giá vốn STH/LTH (ResearchBitcoin),
        Realized P/L + MVRV/NUPL/SOPR (BGeometrics), ghi vào các file JSON,
        gửi tóm tắt Telegram (nếu có cấu hình). Workflow sẽ commit các file này.

Secrets/biến môi trường:
  Streamlit:  GITHUB_REPO (vd "vietquang1105/bitcoin-urpd-dashboard"), GITHUB_BRANCH (mặc định "main"),
              GITHUB_TOKEN (tùy chọn, chỉ cần quyền đọc nếu repo private).
  Actions:    RESEARCHBITCOIN_API_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (tùy chọn),
              TELEGRAM_LOSS_ALERT_PCT (mặc định 50), RB_BACKFILL_DAYS (mặc định 90).
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

APP_VERSION = "V70"
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
def fetch_bitview_urpd(day, agg="lin1000"):
    """URPD từ Bitview / Bitcoin Research Kit. Trả (frame, url, close, total_supply)."""
    base_url = f"https://bitview.space/api/urpd/all/{day}"
    attempts = [(base_url, {"agg": agg, "weight": "raw"}), (base_url, {})]
    errors, response = [], None
    for url, params in attempts:
        try:
            r = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=30)
            if r.status_code == 404:
                errors.append("404: chưa có snapshot")
                continue
            r.raise_for_status()
            payload = r.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("buckets"), list) or not payload["buckets"]:
                raise ValueError("response không có buckets")
            response = (r, payload)
            break
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")
    if response is None:
        raise RuntimeError(f"Bitview không lấy được {day}. " + " | ".join(errors))

    r, payload = response
    use_fixed_width = "agg=lin1000" in r.url
    rows = []
    for b in payload["buckets"]:
        if not isinstance(b, dict):
            continue
        low = safe_float(b.get("price_floor"))
        supply = safe_float(b.get("supply"))
        if low is None or supply is None or supply < 0:
            continue
        rows.append({"price_low": low, "btc_amount": supply,
                     "price_high": low + 1000.0 if use_fixed_width else low})
    if not rows:
        raise ValueError(f"Bitview trả buckets nhưng không có dòng hợp lệ cho {day}.")
    out = normalize_urpd(pd.DataFrame(rows))
    return out, r.url, payload.get("close"), payload.get("total_supply")


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


def build_signals(price, onchain_df, realized_df, cur_frame, frame_7d_ago, ath):
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
        if len(s7) >= 3:
            v = float(s7.sopr.mean())
            rows.append(["SOPR trung bình 7 ngày", light(v, good_above=1.005, bad_below=0.995), f"{v:.3f}",
                         "> 1: coin được bán có lãi. < 1: bán lỗ."])

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
                supports, resistances, events):
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
        # Hôm nay/hôm qua có thể chưa chốt → luôn lấy lại; bù tối đa 30 ngày còn thiếu.
        start = last if last >= today - timedelta(days=1) else last + timedelta(days=1)
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
    if updated:
        data["daily"] = dict(sorted((k, v) for k, v in daily.items() if is_day_key(k))[-DAILY_MAX_DAYS:])
        data["updated_at_utc"] = utc_now().strftime("%Y-%m-%d %H:%M")
        _save_json(RESEARCH_FILE, data)
    status["research"] = {"updated": updated, "errors": errors[-4:]}
    _log(f"ResearchBitcoin: {updated or 'không có gì mới'}; lỗi {errors[-2:] if errors else 0}")


def _update_bg_file(path, slug_to_key, status_key, status, only_if_missing=None):
    """Cập nhật file daily từ BGeometrics; mỗi chỉ số 1 request, chỉ gọi khi còn thiếu ngày."""
    data = _load_json(path)
    data.setdefault("version", 1)
    daily = data.setdefault("daily", {})
    yesterday = utc_today() - timedelta(days=1)
    updated, errors = {}, []
    for slug, key in slug_to_key.items():
        if only_if_missing and not only_if_missing(key):
            continue
        have = _daily_dates(daily, key)
        if have and have[-1] >= day_str(yesterday):
            continue
        start = parse_day(have[-1]) + timedelta(days=1) if have else yesterday - timedelta(days=BG_INITIAL_DAYS - 1)
        try:
            frame = bgeometrics_fetch_daily(slug, start, yesterday)
            for row in frame.itertuples(index=False):
                daily.setdefault(day_str(row.date), {})[key] = float(row.value)
            updated[key] = len(frame)
        except Exception as e:
            errors.append(redact(e)[:200])
        time.sleep(2)
    if updated:
        data["daily"] = dict(sorted((k, v) for k, v in daily.items() if is_day_key(k))[-DAILY_MAX_DAYS:])
        data["updated_at_utc"] = utc_now().strftime("%Y-%m-%d %H:%M")
        _save_json(path, data)
    status[status_key] = {"updated": updated, "errors": errors[-4:]}
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
                    "onchain", status, only_if_missing=rb_missing)


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


def merge_onchain_frames(research_df, bg_df):
    """Gộp ResearchBitcoin (ưu tiên) + BGeometrics theo ngày."""
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
        _, greens, reds, verdict = build_signals(price, onchain, realized, cur, f7, DEFAULT_ATH)

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
                                upcoming_btc_events(7, rows=fetch_economic_calendar_raw()))
            lines += ["• " + _telegram_html(b) for b in brief]
            lines.append(_telegram_html(f"Tín hiệu: **{verdict}** ({greens} 🟢 / {reds} 🔴)"))
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


def run_update():
    _log(f"BTC URPD Monitor {APP_VERSION} — cập nhật dữ liệu")
    status = _load_json(STATUS_FILE)
    for k in ("urpd", "research", "realized", "onchain"):
        status.pop(k, None)
    if isinstance(status.get("telegram"), dict):
        status["telegram"].pop("error", None)
    status["fatal"] = []
    for step in (update_urpd, update_research, update_realized, update_onchain, update_market, send_telegram_report):
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
    frames, prices = {}, {}
    for k, v in raw.items():
        if not is_day_key(k) or not isinstance(v, dict) or not v.get("urpd"):
            continue
        try:
            frames[k] = records_to_urpd(v["urpd"])
            p = safe_float(v.get("price"))
            if p:
                prices[k] = p
        except Exception:
            continue
    dates = sorted(frames)
    return {"frames": frames, "prices": prices, "dates": dates, "source": source,
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
    if not dates or parse_day(dates[-1]) < utc_today() - timedelta(days=1):
        live = live_bitview_latest()
        if live and (not dates or live[0] > dates[-1]):
            return records_to_urpd(live[1]), live[0], live[2], "Bitview trực tiếp (chưa lưu — GitHub Actions chưa chạy)"
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
            for step in ("urpd", "research", "realized", "onchain"):
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
    brief = daily_brief(price, chg, onchain, realized, cur_date, frame, prev_frame, supports, resistances, events)
    mk("\n".join(f"- {b}" for b in brief) if brief else "Chưa đủ dữ liệu.")

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
    mvrv, _ = latest_value(onchain, "mvrv")
    nupl, _ = latest_value(onchain, "nupl")
    c[2].metric("MVRV · NUPL", f"{mvrv:.2f}" if mvrv is not None else "N/A",
                f"NUPL {nupl:.2f}" if nupl is not None else None, delta_color="off")
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
    rows, g, r, verdict = build_signals(price, onchain, realized, frame, f7, ath)
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
                supports=(), resistances=(), ath=DEFAULT_ATH):
    plot = frame[frame.price_low > 0].sort_values("price_low").reset_index(drop=True)
    lo, hi = plot.price_low.to_numpy(float), plot.price_high.to_numpy(float)
    mid = (lo + hi) / 2
    fig = go.Figure()
    if cmp_frame is not None:
        cp = cmp_frame[cmp_frame.price_low > 0]
        fig.add_bar(x=(cp.price_low + cp.price_high) / 2, y=cp.btc_amount, width=(cp.price_high - cp.price_low) * 0.92,
                    name=cmp_label, marker_color="rgba(148,163,184,.28)",
                    marker_line=dict(color="rgba(148,163,184,.7)", width=1),
                    hovertemplate=f"{cmp_label}<br>BTC: %{{y:,.0f}}<extra></extra>")
        bd = bucket_deltas(plot, cmp_frame)
        old = pd.Series(bd.old.values, index=bd.low.values).reindex(lo).to_numpy(float) if not bd.empty else np.full(len(lo), np.nan)
        custom = np.column_stack([lo, hi, old, plot.btc_amount.to_numpy(float) - old])
        hover = (f"{label}<br>$%{{customdata[0]:,.0f}} – $%{{customdata[1]:,.0f}}<br>BTC: %{{y:,.0f}}<br>"
                 f"{cmp_label}: %{{customdata[2]:,.0f}}<br>Thay đổi: %{{customdata[3]:+,.0f}} BTC<extra></extra>")
    else:
        custom = np.column_stack([lo, hi])
        hover = f"{label}<br>$%{{customdata[0]:,.0f}} – $%{{customdata[1]:,.0f}}<br>BTC: %{{y:,.0f}}<extra></extra>"
    fig.add_bar(x=mid, y=plot.btc_amount, width=(hi - lo) * (0.72 if cmp_frame is not None else 0.92),
                name=label, marker_color=np.where(mid < price, GREEN, RED), customdata=custom, hovertemplate=hover)
    for z in supports:
        fig.add_vrect(x0=z["low"], x1=z["high"], fillcolor=GREEN, opacity=0.10, line_width=0, layer="below")
    for z in resistances:
        fig.add_vrect(x0=z["low"], x1=z["high"], fillcolor=RED, opacity=0.10, line_width=0, layer="below")
    fig.add_vline(x=price, line_dash="dash", line_color="white",
                  annotation_text=f"Giá {fmt_usd(price)}", annotation_position="top left")
    if sth:
        fig.add_vline(x=sth, line_dash="dot", line_color=AMBER, annotation_text=f"STH {fmt_usd(sth)}",
                      annotation_position="top right", annotation_y=0.93, annotation_font_color=AMBER)
    if lth:
        fig.add_vline(x=lth, line_dash="dot", line_color=CYAN, annotation_text=f"LTH {fmt_usd(lth)}",
                      annotation_position="top right", annotation_y=0.86, annotation_font_color=CYAN)
    style_fig(fig, 560, hovermode="closest")
    fig.update_layout(barmode="overlay", bargap=0.02, showlegend=cmp_frame is not None,
                      xaxis=dict(title="Giá vốn on-chain (USD)", range=[0, max(ath, price) * 1.05], tickformat="$,.0f"),
                      yaxis=dict(title="BTC"))
    return fig


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
    show_fig(urpd_figure(frame, price, f"Snapshot {sel}", cmp_frame, f"Snapshot {cmp_date}", sth, lth,
                         supports, resistances, ath), key="urpd_main")
    cap("🟢 giá vốn dưới giá snapshot · 🔴 trên giá · nền xanh/đỏ = vùng hỗ trợ/kháng cự tự động · "
               "chấm cam/xanh = giá vốn STH/LTH.")

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

    # ---- Bucket thay đổi mạnh nhất
    if cmp_frame is not None:
        bd = bucket_deltas(frame, cmp_frame)
        if not bd.empty:
            st.subheader(f"🔄 Bucket thay đổi mạnh nhất: {cmp_date} → {sel}")
            up, dn = bd[bd.delta > 0], bd[bd.delta < 0]
            m = st.columns(3)
            m[0].metric("Tổng bucket tăng", fmt_btc(up.delta.sum()))
            m[1].metric("Tổng bucket giảm", fmt_btc(dn.delta.sum()))
            m[2].metric("Chênh lệch ròng", fmt_btc(bd.delta.sum(), signed=True))

            def top(df, asc):
                x = df.sort_values("delta", ascending=asc).head(10)
                return pd.DataFrame({"Vùng giá": [f"{fmt_usd(a)} – {fmt_usd(b)}" for a, b in zip(x.low, x.high)],
                                     "Thay đổi": [fmt_btc(v, signed=True) for v in x.delta],
                                     "%": [f"{v:+.1f}%" if np.isfinite(v) else "—" for v in x.pct]})
            a, b = st.columns(2)
            with a:
                mk("**🟢 Tăng nhiều nhất**")
                show_df(top(up, False))
            with b:
                mk("**🔴 Giảm nhiều nhất**")
                show_df(top(dn, True))
            st.download_button("⬇️ Tải CSV toàn bộ bucket", bd.to_csv(index=False).encode("utf-8-sig"),
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
                   "SOPR < 1 kéo dài: thị trường đang bán lỗ.")


# =============================================================================
# TRANG 4 — LỊCH SỬ (heatmap + xu hướng)
# =============================================================================
@st.cache_data(ttl=900, show_spinner="Đang dựng bản đồ nhiệt…")
def heatmap_cached(version, days):
    store = get_urpd_store()
    return build_heatmap_data(store["frames"], store["prices"], days)


def page_history():
    ath = ath_value()
    store = get_urpd_store()
    st.title("🗓️ Lịch sử nguồn cung")
    if len(store["dates"]) < 2:
        st.info("Cần ít nhất 2 snapshot URPD trong history.")
        return
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
# ĐIỂM VÀO
# =============================================================================
BTC_PAGES = [
    (page_overview, "Tổng quan", "📊", "tong-quan"),
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
