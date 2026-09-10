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

st.set_page_config(layout="wide", page_title="BTC URPD Monitor V30", page_icon="🪙")

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
NEWS_CHECK_MINUTES = 15
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
            "btc_amount",
            "btc",
            "amount",
            "supply",
            "realized_supply",
            "quantity",
            "btcsupply",
        ],
    )

    if low is None or btc is None:
        raise ValueError(f"Không nhận diện được cột URPD: {list(df.columns)}")

    out = pd.DataFrame(
        {
            "price_low": pd.to_numeric(df[low], errors="coerce"),
            "btc_amount": pd.to_numeric(df[btc], errors="coerce"),
        }
    )
    out["price_high"] = (
        pd.to_numeric(df[high], errors="coerce")
        if high is not None
        else out["price_low"]
    )

    out = out.replace([np.inf, -np.inf], np.nan).dropna()
    out = out[out.btc_amount >= 0].copy()

    # Giữ nguyên bucket giá 0 trong dữ liệu gốc.
    # Bucket này chỉ được ẩn khi vẽ biểu đồ để tránh kéo lệch khung nhìn.
    # Tổng URPD và các phép tính nguồn cung vẫn dùng đủ dữ liệu gốc.

    # Một số API trả satoshi thay vì BTC.
    if not out.empty and out.btc_amount.max() > 2.1e15:
        out.btc_amount /= 100_000_000

    # Nếu API chỉ trả một mức giá cho mỗi bucket, dựng biên bucket.
    if (out.price_low == out.price_high).all() and len(out) > 1:
        out = out.sort_values("price_low").reset_index(drop=True)
        p = out.price_low.to_numpy()
        e = np.empty(len(p) + 1)
        e[1:-1] = (p[:-1] + p[1:]) / 2
        e[0] = max(0, p[0] - (e[1] - p[0]))
        e[-1] = p[-1] + (p[-1] - e[-2])
        out.price_low, out.price_high = e[:-1], e[1:]

    out = out[out.price_high > out.price_low]
    if out.empty:
        raise ValueError("Không có bucket URPD hợp lệ.")

    return out.sort_values("price_low").reset_index(drop=True)


def btc_in_range(df, low, high):
    lo = np.maximum(df.price_low.to_numpy(float), low)
    hi = np.minimum(df.price_high.to_numpy(float), high)
    width = df.price_high.to_numpy(float) - df.price_low.to_numpy(float)
    overlap = np.maximum(0, hi - lo)
    return float(np.sum(df.btc_amount.to_numpy(float) * overlap / width))


@st.cache_data(ttl=300)
def market_overview():
    """
    Lấy vốn hóa toàn thị trường và vốn hóa BTC/USDT/USDC từ CoinGecko.
    BTC dominance điều chỉnh được tính:
        Market Cap BTC / (Tổng Market Cap - Market Cap USDT - Market Cap USDC)
    Như vậy USDT và USDC không được tính vào mẫu số của dominance.
    """
    global_url = "https://api.coingecko.com/api/v3/global"
    simple_url = "https://api.coingecko.com/api/v3/simple/price"
    headers = {"Accept": "application/json", "User-Agent": "BTC-URPD-Dashboard/27"}

    r_global = requests.get(global_url, headers=headers, timeout=20)
    r_global.raise_for_status()
    g = r_global.json().get("data", {})
    total_mcap = float(g.get("total_market_cap", {}).get("usd"))
    total_mcap_change = g.get("market_cap_change_percentage_24h_usd")

    params = {
        "ids": "bitcoin,tether,usd-coin",
        "vs_currencies": "usd",
        "include_market_cap": "true",
        "include_24hr_change": "true",
    }
    r_simple = requests.get(simple_url, params=params, headers=headers, timeout=20)
    r_simple.raise_for_status()
    data = r_simple.json()

    btc_mcap = float(data["bitcoin"]["usd_market_cap"])
    usdt_mcap = float(data["tether"]["usd_market_cap"])
    usdc_mcap = float(data["usd-coin"]["usd_market_cap"])

    btc_mcap_change = data["bitcoin"].get("usd_24h_change")
    usdt_mcap_change = data["tether"].get("usd_24h_change")
    usdc_mcap_change = data["usd-coin"].get("usd_24h_change")

    adjusted_total = total_mcap - usdt_mcap - usdc_mcap
    adjusted_btc_dom = (btc_mcap / adjusted_total * 100.0) if adjusted_total > 0 else None
    alt_ex_stables = total_mcap - btc_mcap - usdt_mcap - usdc_mcap

    # Ước tính biến động 24h của Altcoin (loại BTC, USDT, USDC)
    # bằng cách dựng vốn hóa đầu kỳ từ % thay đổi 24h của từng thành phần.
    def previous_cap(current, pct_change):
        try:
            pct = float(pct_change)
            denom = 1.0 + pct / 100.0
            return current / denom if denom > 0 else None
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    btc_prev = previous_cap(btc_mcap, btc_mcap_change)
    usdt_prev = previous_cap(usdt_mcap, usdt_mcap_change)
    usdc_prev = previous_cap(usdc_mcap, usdc_mcap_change)

    # CoinGecko global endpoint cung cấp % thay đổi vốn hóa toàn thị trường 24h.
    total_prev = previous_cap(total_mcap, total_mcap_change)
    alt_prev = None
    alt_change = None
    if all(v is not None for v in (total_prev, btc_prev, usdt_prev, usdc_prev)):
        alt_prev = total_prev - btc_prev - usdt_prev - usdc_prev
        if alt_prev > 0:
            alt_change = (alt_ex_stables / alt_prev - 1.0) * 100.0

    return {
        "total_mcap": total_mcap,
        "total_mcap_change_24h": float(total_mcap_change) if total_mcap_change is not None else None,
        "btc_mcap": btc_mcap,
        "btc_mcap_change_24h": float(btc_mcap_change) if btc_mcap_change is not None else None,
        "usdt_mcap": usdt_mcap,
        "usdc_mcap": usdc_mcap,
        "adjusted_total": adjusted_total,
        "adjusted_btc_dom": adjusted_btc_dom,
        "alt_ex_stables": alt_ex_stables,
        "alt_ex_stables_change_24h": float(alt_change) if alt_change is not None else None,
    }


@st.cache_data(ttl=300)
def btc_price():
    for url in [
        "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
        "https://api.coinbase.com/v2/prices/BTC-USD/spot",
    ]:
        try:
            j = requests.get(url, timeout=15).json()
            if "bitcoin" in j:
                return float(j["bitcoin"]["usd"])
            return float(j["data"]["amount"])
        except Exception:
            pass
    return None


@st.cache_data(ttl=1800)
def bgeometrics_urpd(day):
    """
    Lấy URPD miễn phí từ BGeometrics.
    BGeometrics chưa có dữ liệu ngày hiện tại thì dùng ngày hôm qua.
    Endpoint trả các trường: priceLower, priceUpper, btcSupply, pctSupply.
    """
    url = "https://bitcoin-data.com/v1/urpd"
    params = {"day": day}
    headers = {"Accept": "application/hal+json"}

    r = requests.get(url, params=params, headers=headers, timeout=30)
    if r.status_code == 429:
        raise RuntimeError("BGeometrics đang giới hạn lượt gọi.")
    r.raise_for_status()

    payload = r.json()
    if not isinstance(payload, list):
        for key in ("data", "result", "rows", "values"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break

    if not isinstance(payload, list) or not payload:
        raise ValueError(f"BGeometrics không trả URPD cho ngày {day}.")

    return normalize_urpd(payload), r.url


@st.cache_data(ttl=1800)
def bitview_urpd(day, agg="lin1000"):
    """
    Lấy URPD từ Bitcoin Research Kit / Bitview.
    Ưu tiên bucket lin1000; nếu request có tham số aggregate bị từ chối,
    tự động thử endpoint mặc định raw rồi dựng bucket từ các price_floor.
    """
    base_url = f"https://bitview.space/api/urpd/all/{day}"
    headers = {"Accept": "application/json"}

    attempts = [
        (base_url, {"agg": agg, "weight": "raw"}),
        (base_url, {}),
    ]
    errors = []
    response = None

    for url, params in attempts:
        try:
            r = requests.get(url, params=params, headers=headers, timeout=30)
            if r.status_code == 404:
                errors.append("404: không có snapshot")
                continue
            r.raise_for_status()
            payload = r.json()
            if not isinstance(payload, dict):
                raise ValueError("response không phải object JSON")
            buckets = payload.get("buckets")
            if not isinstance(buckets, list) or not buckets:
                raise ValueError("response không có buckets")
            response = (r, payload)
            break
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")

    if response is None:
        raise RuntimeError(f"Bitview không lấy được {day}. " + " | ".join(errors))

    r, payload = response
    buckets = payload["buckets"]

    rows = []
    # Nếu server trả đúng lin1000 thì mỗi bucket rộng $1,000.
    # Nếu fallback về raw, dùng khoảng giữa các price_floor để dựng biên.
    use_fixed_width = bool(r.url.find("agg=lin1000") >= 0)

    for b in buckets:
        if not isinstance(b, dict):
            continue
        low = pd.to_numeric(b.get("price_floor"), errors="coerce")
        supply = pd.to_numeric(b.get("supply"), errors="coerce")
        if pd.isna(low) or pd.isna(supply) or float(supply) < 0:
            continue
        rows.append({
            "price_low": float(low),
            "btc_amount": float(supply),
            "price_high": float(low) + 1000.0 if use_fixed_width else float(low),
        })

    if not rows:
        raise ValueError(f"Bitview trả buckets nhưng không có dòng URPD hợp lệ cho {day}.")

    out = pd.DataFrame(rows)
    if not use_fixed_width:
        out = out.sort_values("price_low").reset_index(drop=True)
        p = out["price_low"].to_numpy(float)
        if len(p) == 1:
            out["price_high"] = p + 1.0
        else:
            edges = np.empty(len(p) + 1)
            edges[1:-1] = (p[:-1] + p[1:]) / 2
            edges[0] = max(0.0, p[0] - (edges[1] - p[0]))
            edges[-1] = p[-1] + (p[-1] - edges[-2])
            out["price_low"] = edges[:-1]
            out["price_high"] = edges[1:]

    out = normalize_urpd(out)
    return out, r.url, payload.get("close"), payload.get("total_supply")



@st.cache_data(ttl=1800, show_spinner=False)
def bitview_period_snapshots(start_date, end_date, mode_days, ath_value, bottom_start_value, bottom_end_value):
    """Lấy chuỗi snapshot cho báo cáo xu hướng.

    1–30 ngày: lấy từng ngày.
    31–180 ngày: lấy mốc đầu/cuối và các mốc cách nhau 7 ngày để giảm
    số request. Các snapshot đã được cache ở bitview_urpd().
    """
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    if end < start:
        return pd.DataFrame(), {}

    dates = []
    if mode_days <= 30:
        d = start
        while d <= end:
            dates.append(d)
            d += timedelta(days=1)
    else:
        dates.append(start)
        d = start + timedelta(days=7)
        while d < end:
            dates.append(d)
            d += timedelta(days=7)
        if dates[-1] != end:
            dates.append(end)

    snapshots = {}
    rows = []
    for d in dates:
        day = d.strftime("%Y-%m-%d")
        try:
            frame, url, close, total_supply = bitview_urpd(day)
            px = float(close) if close is not None else None
            if px is None or px <= 0:
                continue
            total = float(frame.btc_amount.sum())
            overhead = btc_in_range(frame, px, float(ath_value))
            bottom = btc_in_range(frame, float(bottom_start_value), float(bottom_end_value))
            near_low = max(0.0, px * 0.90)
            near_high = px * 1.10
            support = btc_in_range(frame, near_low, px)
            resistance = btc_in_range(frame, px, near_high)
            below = float(frame.loc[frame.price_high <= px, "btc_amount"].sum())
            snapshots[day] = frame
            rows.append({
                "date": day,
                "price": px,
                "total_urpd": total,
                "below_price": below,
                "overhead_ath": overhead,
                "bottom_supply": bottom,
                "support_near": support,
                "resistance_near": resistance,
                "sr_ratio": (support / resistance) if resistance > 0 else np.nan,
                "url": url,
                "bitview_total_supply": float(total_supply) if total_supply is not None else np.nan,
            })
        except Exception:
            continue

    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True) if rows else pd.DataFrame()
    return df, snapshots

def extract_scalar(payload):
    if isinstance(payload, (int, float)):
        return float(payload)

    if isinstance(payload, list):
        if not payload:
            raise ValueError("API trả về danh sách rỗng.")
        return extract_scalar(payload[-1])

    if isinstance(payload, dict):
        for key in ("data", "result", "rows", "values", "results"):
            if key in payload:
                return extract_scalar(payload[key])

        for key in (
            "value",
            "val",
            "supply_in_loss",
            "supply_in_loss_percent",
            "metric_value",
        ):
            if key in payload and payload[key] is not None:
                return float(payload[key])

        for value in reversed(list(payload.values())):
            try:
                return extract_scalar(value)
            except (TypeError, ValueError, KeyError):
                continue

    raise ValueError(f"Không đọc được giá trị từ phản hồi API: {payload}")


@st.cache_data(ttl=300)
def researchbitcoin_metric(token, metric="supply_in_loss", resolution="d1"):
    if not token:
        raise RuntimeError("Chưa nhập ResearchBitcoin API token.")

    url = f"https://api.researchbitcoin.net/v2/supply_in_profitloss/{metric}"
    headers = {
        "Accept": "application/json",
        "X-API-Token": token.strip(),
    }
    params = {"resolution": resolution, "output_format": "json"}

    r = requests.get(url, headers=headers, params=params, timeout=30)
    if r.status_code == 401:
        raise RuntimeError("API token không hợp lệ hoặc đã hết hạn (401).")
    if r.status_code == 403:
        raise RuntimeError("Token không có quyền truy cập chỉ số này (403).")
    if r.status_code == 429:
        raise RuntimeError("API đang giới hạn lượt gọi (429).")
    r.raise_for_status()

    payload = r.json()
    value = extract_scalar(payload)
    return float(value), r.url


st.title("Phân bố Nguồn cung Bitcoin theo Giá vốn (URPD)")
st.caption("URPD thực tế từ Bitview • BGeometrics dự phòng • Supply in Loss trực tiếp • News Radar • Không mô phỏng")

with st.sidebar:
    st.header("Thiết lập")
    token = st.secrets.get("RESEARCHBITCOIN_API_TOKEN", "")
    if token:
        st.success("ResearchBitcoin API: đã kết nối")
    else:
        st.warning("ResearchBitcoin API: chưa có token")
    uploaded = st.file_uploader(
        "Hoặc tải URPD CSV/Excel thực tế", type=["csv", "xlsx"]
    )
    top_start = st.number_input(
        "Ngưỡng tham khảo vùng giá cao (USD)", value=TOP_START, step=100.0
    )
    bottom_start = st.number_input(
        "Vùng đáy từ (USD)", value=BOTTOM_START, step=100.0
    )
    ath = st.number_input("ATH (USD)", value=DEFAULT_ATH, step=100.0)

# =========================
# HISTORY: lưu bền vững trên GitHub
# =========================
# Streamlit Cloud có filesystem tạm thời, vì vậy chỉ ghi urpd_history.json
# tại máy chạy app là chưa đủ. Phần history dùng GitHub làm nơi lưu chính.
history_file = "urpd_history.json"
github_token = st.secrets.get("GITHUB_TOKEN", "")
github_repo = st.secrets.get("GITHUB_REPO", "")
github_branch = st.secrets.get("GITHUB_BRANCH", "main")

def github_history_get():
    """Đọc urpd_history.json từ GitHub. Nếu chưa cấu hình GitHub thì đọc file local."""
    if not github_token or not github_repo:
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}, None
        except Exception:
            return {}, None

    url = f"https://api.github.com/repos/{github_repo}/contents/{history_file}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {github_token.strip()}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        r = requests.get(
            url,
            headers=headers,
            params={"ref": github_branch},
            timeout=20,
        )
        if r.status_code == 404:
            return {}, None
        r.raise_for_status()
        payload = r.json()
        content = base64.b64decode(payload.get("content", "")).decode("utf-8")
        data = json.loads(content)
        return (data if isinstance(data, dict) else {}), payload.get("sha")
    except Exception as e:
        st.warning(f"Không đọc được history từ GitHub: {e}")
        # Fallback sang file local nếu có.
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return (data if isinstance(data, dict) else {}), None
        except Exception:
            return {}, None

def github_history_save(data, sha=None):
    """Lưu toàn bộ history lên GitHub, giữ nguyên các ngày cũ."""
    if not github_token or not github_repo:
        with open(history_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True

    url = f"https://api.github.com/repos/{github_repo}/contents/{history_file}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {github_token.strip()}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    payload = {
        "message": "Update URPD history",
        "content": base64.b64encode(body).decode("ascii"),
        "branch": github_branch,
    }
    if sha:
        payload["sha"] = sha

    try:
        r = requests.put(url, headers=headers, json=payload, timeout=30)
        if r.status_code == 409:
            # File đã đổi trên GitHub: đọc lại SHA rồi thử đúng 1 lần.
            fresh_data, fresh_sha = github_history_get()
            if fresh_sha:
                payload["sha"] = fresh_sha
                r = requests.put(url, headers=headers, json=payload, timeout=30)
        r.raise_for_status()

        # Ghi local luôn để local chạy cũng giữ được bản mới.
        with open(history_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        st.warning(f"Không lưu được history lên GitHub: {e}")
        # Vẫn lưu local để không mất snapshot trong phiên chạy.
        try:
            with open(history_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return False


# =========================
# NEWS RADAR: tin tức BTC / vĩ mô / chính sách
# =========================
# Dùng Google News RSS để lấy tiêu đề mới; không cần API key.
# Bộ lọc ưu tiên các chủ đề có khả năng tác động trực tiếp hoặc gián tiếp tới BTC.
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html import unescape
import re

NEWS_QUERIES = [
    ("Bitcoin", 'Bitcoin BTC'),
    ("Fed / lãi suất", 'Bitcoin Fed OR FOMC OR "interest rate"'),
    ("Lạm phát Mỹ", 'Bitcoin CPI OR PPI OR inflation'),
    ("ETF / dòng vốn", 'Bitcoin ETF OR "spot bitcoin ETF"'),
    ("Thanh khoản / trái phiếu", 'Bitcoin Treasury yields OR dollar OR liquidity'),
    ("Quy định crypto", 'Bitcoin SEC OR CFTC OR crypto regulation OR Clarity Act'),
    ("Địa chính trị / dầu", 'Bitcoin oil OR Iran OR Middle East OR geopolitics'),
]

@st.cache_data(ttl=900, show_spinner=False)
def fetch_news_radar(days=7, max_items=30):
    """Lấy tin 7 ngày gần nhất từ Google News RSS; thất bại nguồn nào thì bỏ qua."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    rows = []
    seen = set()
    headers = {"User-Agent": "Mozilla/5.0 BTC-URPD-News-Radar/28"}

    for category, query in NEWS_QUERIES:
        rss_url = (
            "https://news.google.com/rss/search?q="
            + quote_plus(query)
            + "&hl=en-US&gl=US&ceid=US:en"
        )
        try:
            r = requests.get(rss_url, headers=headers, timeout=15)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception:
            continue

        for item in root.findall("./channel/item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub_raw = (item.findtext("pubDate") or "").strip()
            source_node = item.find("source")
            source = (source_node.text or "").strip() if source_node is not None else ""
            desc = (item.findtext("description") or "").strip()
            if not title or not link:
                continue
            try:
                published = parsedate_to_datetime(pub_raw).astimezone(timezone.utc)
            except Exception:
                published = now
            if published < cutoff:
                continue
            key = title.lower()
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "title": title,
                "link": link,
                "source": source or "Google News",
                "category": category,
                "published": published,
                "description": desc,
            })

    rows.sort(key=lambda x: x["published"], reverse=True)
    return rows[:max_items]


@st.cache_data(ttl=86400, show_spinner=False)
def translate_to_vietnamese(text):
    """Dịch tiêu đề/mô tả tiếng Anh sang tiếng Việt bằng endpoint Google Translate công khai.
    Có cache 24h để cùng một tin không bị dịch lại mỗi 15 phút.
    Nếu dịch lỗi, giữ nguyên văn bản gốc.
    """
    text = unescape(re.sub(r"<[^>]+>", " ", str(text or "")))
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    if len(text) > 1800:
        text = text[:1800].rsplit(" ", 1)[0] + "…"
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": "en",
            "tl": "vi",
            "dt": "t",
            "q": text,
        }
        r = requests.get(
            url,
            params=params,
            headers={"User-Agent": "Mozilla/5.0 BTC-URPD-News-Radar/28"},
            timeout=12,
        )
        r.raise_for_status()
        payload = r.json()
        translated = "".join(part[0] for part in payload[0] if part and part[0])
        return translated.strip() or text
    except Exception:
        return text


def news_title_vi(item):
    """Tiêu đề tiếng Việt hiển thị chính; luôn có fallback tiếng Anh."""
    title = item.get("title", "")
    return translate_to_vietnamese(title) or title


def news_summary_vi(item):
    """Dịch mô tả RSS thành tóm tắt ngắn; nếu không có thì bỏ qua."""
    desc = item.get("description", "")
    if not desc:
        return ""
    cleaned = unescape(re.sub(r"<[^>]+>", " ", desc))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > 420:
        cleaned = cleaned[:420].rsplit(" ", 1)[0] + "…"
    return translate_to_vietnamese(cleaned)

def news_impact(title, category):
    """Gắn nhãn định hướng đơn giản, minh bạch; không coi đây là dự báo giá."""
    t = title.lower()
    negative_terms = (
        "rate hike", "hike rates", "higher rates", "hawkish", "hot inflation",
        "inflation rises", "cpi rises", "ppi rises", "yield rises", "yields rise",
        "strong jobs", "oil surges", "oil rises", "risk-off", "selloff", "sell-off",
        "war", "iran", "outflows", "outflow", "crackdown", "ban", "lawsuit",
    )
    positive_terms = (
        "rate cut", "cut rates", "lower rates", "dovish", "cooling inflation",
        "inflation cools", "cpi falls", "ppi falls", "yield falls", "yields fall",
        "etf inflow", "inflows", "institutional demand", "approval", "clarity",
        "regulatory clarity", "bullish", "buying",
    )
    if any(x in t for x in negative_terms):
        direction = "🔴 Bất lợi ngắn hạn"
    elif any(x in t for x in positive_terms):
        direction = "🟢 Có lợi ngắn hạn"
    else:
        direction = "🟡 Chưa rõ"

    direct_categories = {"Fed / lãi suất", "Lạm phát Mỹ", "ETF / dòng vốn"}
    impact = "Trực tiếp" if category in direct_categories else "Gián tiếp"
    return impact, direction


def upcoming_btc_events(days=7):
    """Lịch sự kiện trọng yếu có sẵn từ lịch chính thức năm 2026, lọc 7 ngày tới."""
    today = datetime.now(timezone.utc).date()
    end = today + timedelta(days=days)
    events = [
        {"date": "2026-09-11", "time": "08:30 ET", "event": "CPI Mỹ tháng 8", "impact": "🔴🔴 Rất cao", "why": "Quyết định kỳ vọng Fed; CPI nóng thường gây áp lực lên BTC qua lợi suất/USD."},
        {"date": "2026-09-16", "time": "08:30 ET", "event": "Retail Sales Mỹ tháng 8", "impact": "🟠 Cao", "why": "Đo sức khỏe tiêu dùng; quá mạnh có thể giữ chính sách tiền tệ chặt hơn."},
        {"date": "2026-09-16", "time": "08:30 ET", "event": "US Import / Export Price Indexes", "impact": "🟠 Cao", "why": "Thêm tín hiệu lạm phát trước/đồng thời với quyết định Fed."},
        {"date": "2026-09-16", "time": "14:00 ET", "event": "FOMC — quyết định lãi suất", "impact": "🔴🔴 Rất cao", "why": "Catalyst vĩ mô lớn nhất tuần; thay đổi lãi suất và thông điệp Fed có thể làm BTC biến động mạnh."},
        {"date": "2026-09-16", "time": "14:30 ET", "event": "Họp báo Chủ tịch Fed", "impact": "🔴🔴 Rất cao", "why": "Giọng điệu về lạm phát và đường đi lãi suất thường quan trọng không kém quyết định lãi suất."},
        {"date": "2026-09-17", "time": "08:30 ET", "event": "Initial Jobless Claims", "impact": "🟠 Cao", "why": "Tín hiệu nhanh về thị trường lao động; ảnh hưởng kỳ vọng chính sách Fed."},
        {"date": "2026-09-17", "time": "08:30 ET", "event": "Housing Starts / Building Permits", "impact": "🟡 Vừa", "why": "Tín hiệu chu kỳ kinh tế và tăng trưởng Mỹ."},
        {"date": "2026-09-17", "time": "08:30 ET", "event": "Philadelphia Fed Manufacturing", "impact": "🟡 Vừa", "why": "Tín hiệu sớm về hoạt động sản xuất và tăng trưởng."},
    ]
    out = []
    for e in events:
        d = datetime.strptime(e["date"], "%Y-%m-%d").date()
        if today <= d <= end:
            e = dict(e)
            e["date_obj"] = d
            out.append(e)
    return out

history, history_sha = github_history_get()

# Vùng đáy đến do người dùng tự thiết lập.
with st.sidebar:
    bottom_end = st.number_input(
        "Vùng đáy đến (USD)",
        value=st.session_state.get("bottom_end_manual", BOTTOM_END),
        min_value=float(bottom_start),
        step=100.0,
        format="%.2f",
        key="bottom_end_manual",
    )

with st.sidebar:
    st.markdown("### Dữ liệu URPD")
    refresh_api = st.button("🔄 Cập nhật URPD mới", use_container_width=True)
    auto_refresh_enabled = st.checkbox(
        "🔄 Tự động kiểm tra snapshot URPD mỗi 1 giờ",
        value=True,
        help=(
            "Mỗi 1 giờ app sẽ kiểm tra ngày URPD mới. Nếu history đã có ngày đó "
            "thì không gọi Bitview lại; nếu chưa có thì chỉ lấy snapshot còn thiếu."
        ),
    )
    news_auto_refresh_enabled = st.checkbox(
        "📰 Tự động cập nhật tin mỗi 15 phút",
        value=True,
        help=(
            "Mỗi 15 phút dashboard tự rerun để lấy tin mới. News Radar dùng cache 15 phút; "
            "việc này không gọi lại Bitview trừ khi đến chu kỳ kiểm tra URPD 1 giờ."
        ),
    )
    if refresh_api:
        st.cache_data.clear()
        st.session_state["force_urpd_refresh"] = True

force_refresh = st.session_state.pop("force_urpd_refresh", False)

# Tự động kiểm tra tối đa 1 lần/giờ trong cùng phiên. Việc refresh trang chỉ là
# cơ chế đánh thức app; logic bên dưới vẫn quyết định có cần gọi Bitview hay không.
now_utc = datetime.now(timezone.utc)
last_auto_check_raw = st.session_state.get("last_auto_check_utc")
try:
    last_auto_check = (
        datetime.fromisoformat(last_auto_check_raw) if last_auto_check_raw else None
    )
except Exception:
    last_auto_check = None

auto_check_due = (
    auto_refresh_enabled
    and (last_auto_check is None or (now_utc - last_auto_check).total_seconds() >= URPD_CHECK_SECONDS)
)
should_check_new_snapshot = bool(force_refresh or auto_check_due)
if should_check_new_snapshot:
    st.session_state["last_auto_check_utc"] = now_utc.isoformat()

# V27: trang tự rerun mỗi 15 phút để News Radar có cơ hội lấy headline mới.
# Nếu tắt News nhưng vẫn bật URPD, trang chỉ cần thức dậy mỗi 1 giờ.
if news_auto_refresh_enabled:
    page_reload_seconds = NEWS_RELOAD_SECONDS
    page_reload_label = f"tin tức {NEWS_CHECK_MINUTES} phút"
elif auto_refresh_enabled:
    page_reload_seconds = URPD_CHECK_SECONDS
    page_reload_label = f"URPD {URPD_CHECK_MINUTES} phút"
else:
    page_reload_seconds = 0
    page_reload_label = "tắt tự động"

if page_reload_seconds > 0:
    components.html(
        f"""
        <script>
        setTimeout(function() {{
            try {{
                window.top.location.reload();
            }} catch (e) {{
                try {{ window.parent.location.reload(); }} catch (e2) {{}}
            }}
        }}, {page_reload_seconds * 1000});
        </script>
        """,
        height=1,
    )

urpd = None
urpd_source = "Chưa có dữ liệu URPD"
urpd_date = None

# Ưu tiên file người dùng tải lên.
if uploaded:
    try:
        raw = (
            pd.read_csv(uploaded)
            if uploaded.name.lower().endswith(".csv")
            else pd.read_excel(uploaded)
        )
        urpd = normalize_urpd(raw)
        urpd_source = uploaded.name
        urpd_date = "Theo file tải lên"
    except Exception as e:
        st.error(f"Lỗi URPD từ file: {e}")

# Nếu không có file, ưu tiên dùng dữ liệu đã lưu. API chỉ được gọi khi:
# 1) Chưa có history; hoặc 2) đến chu kỳ kiểm tra tự động 1 giờ; hoặc
# 3) người dùng bấm nút cập nhật. Khi cần cập nhật, Bitview chỉ được dùng
# để bù các ngày còn thiếu kể từ snapshot gần nhất (tối đa 7 ngày/lần chạy).
history_should_save = False
pending_history_snapshots = {}

if urpd is None:
    saved_dates = sorted(
        k for k, v in history.items()
        if isinstance(k, str) and len(k) == 10 and isinstance(v, dict) and v.get("urpd")
    )
    latest_saved_date = saved_dates[-1] if saved_dates else None

    if latest_saved_date and not should_check_new_snapshot:
        saved_latest = history[latest_saved_date]
        urpd = normalize_urpd(pd.DataFrame(saved_latest["urpd"]))
        urpd_source = saved_latest.get("source", "Lịch sử cục bộ")
        urpd_date = latest_saved_date
        st.info(f"Đang dùng dữ liệu URPD đã lưu ngày {urpd_date}. Rerun không gọi API lại.")
    else:
        # Bitview có thể đã phát hành snapshot của chính ngày UTC hiện tại.
        # Vì vậy mốc cập nhật phải chạy đến HÔM NAY, không dừng ở hôm qua.
        target_end_date = datetime.now(timezone.utc).date()

        if latest_saved_date:
            start_date = datetime.strptime(latest_saved_date, "%Y-%m-%d").date() + timedelta(days=1)
        else:
            start_date = target_end_date

        # Không gọi quá 7 ngày trong một lần cập nhật.
        if start_date < target_end_date - timedelta(days=6):
            start_date = target_end_date - timedelta(days=6)

        dates_to_fetch = []
        d = start_date
        while d <= target_end_date:
            dates_to_fetch.append(d.strftime("%Y-%m-%d"))
            d += timedelta(days=1)

        fetched = []

        for target_date in dates_to_fetch:
            try:
                fetched_urpd, fetched_url, fetched_close, fetched_total_supply = bitview_urpd(target_date)
                snapshot_price = float(fetched_close) if fetched_close is not None else None
                snapshot_top = (
                    btc_in_range(fetched_urpd, snapshot_price, float(ath))
                    if snapshot_price is not None else None
                )
                snapshot_bottom = (
                    btc_in_range(fetched_urpd, float(bottom_start), float(bottom_end))
                    if snapshot_price is not None else None
                )

                pending_history_snapshots[target_date] = {
                    "date": target_date,
                    "price": snapshot_price,
                    "top_btc": float(snapshot_top) if snapshot_top is not None else None,
                    "above_price_btc": float(snapshot_top) if snapshot_top is not None else None,
                    "bottom_btc": float(snapshot_bottom) if snapshot_bottom is not None else None,
                    "total_urpd": float(fetched_urpd.btc_amount.sum()),
                    "urpd": urpd_to_records(fetched_urpd) if "urpd_to_records" in globals() else fetched_urpd[
                        ["price_low", "price_high", "btc_amount"]
                    ].to_dict("records"),
                    "source": "Bitview /api/urpd/all (lin1000)",
                }
                fetched.append((target_date, fetched_urpd, fetched_url, fetched_close, fetched_total_supply))
            except Exception as e:
                st.warning(f"Bitview chưa có URPD ngày {target_date}: {e}")

        if fetched:
            # Dùng ngày mới nhất lấy được làm dữ liệu hiện tại.
            latest_target, urpd, urpd_url, bitview_close, bitview_total_supply = fetched[-1]
            urpd_source = "Bitview /api/urpd/all (lin1000)"
            urpd_date = latest_target
            history_should_save = True
            st.success(
                f"Đã lấy {len(fetched)} snapshot URPD mới từ Bitview: "
                f"{fetched[0][0]} → {fetched[-1][0]}."
            )
        else:
            # Không có ngày nào thiếu: history đã cập nhật đến hiện tại.
            # Hoặc Bitview chưa phát hành snapshot mới: giữ dữ liệu cũ, không tạo ngày giả.
            if latest_saved_date:
                saved_latest = history[latest_saved_date]
                urpd = normalize_urpd(pd.DataFrame(saved_latest["urpd"]))
                urpd_source = saved_latest.get("source", "Lịch sử cục bộ")
                urpd_date = latest_saved_date
                if dates_to_fetch:
                    st.warning(
                        f"Bitview chưa trả được snapshot mới. "
                        f"Đang giữ snapshot gần nhất {latest_saved_date}; không tạo snapshot mới."
                    )
                else:
                    st.info(
                        f"History đã có snapshot mới nhất {latest_saved_date}. "
                        "Không gọi lại URPD của ngày đã lưu."
                    )
            else:
                # Chỉ khi chưa có history nào, thử BGeometrics làm nguồn dự phòng.
                try:
                    urpd, urpd_url = bgeometrics_urpd(target_end_date.strftime("%Y-%m-%d"))
                    urpd_source = "BGeometrics /v1/urpd (dự phòng)"
                    urpd_date = target_end_date.strftime("%Y-%m-%d")
                    history_should_save = True
                    st.success(
                        f"Bitview chưa trả dữ liệu; đã lấy được URPD "
                        f"{urpd_date} từ BGeometrics."
                    )
                except Exception as fallback_error:
                    st.warning(
                        f"Chưa lấy được URPD từ Bitview. "
                        f"BGeometrics dự phòng cũng không lấy được: {fallback_error}"
                    )

if auto_refresh_enabled and not history_should_save:
    st.caption(
        f"🔄 URPD: kiểm tra snapshot mới mỗi {URPD_CHECK_MINUTES} phút; "
        "nếu đã có ngày mới trong history thì không gọi lại Bitview."
    )
elif auto_refresh_enabled and history_should_save:
    st.caption("🟢 URPD: đã kiểm tra tự động và phát hiện snapshot mới; history đã được cập nhật.")
if news_auto_refresh_enabled:
    st.caption(f"📰 News Radar: tự rerun mỗi {NEWS_CHECK_MINUTES} phút; dữ liệu tin được cache {NEWS_CHECK_MINUTES} phút.")

price = btc_price()
if price is None:
    price = st.number_input("Nhập giá BTC hiện tại", value=78818.0)
    price_source = "Giá nhập thủ công"
else:
    price_source = "Giá thị trường trực tiếp"

market = None
market_error = None
try:
    market = market_overview()
except Exception as e:
    market_error = str(e)

loss_btc = None
loss_percent = None
loss_source = ""
if token:
    try:
        loss_btc, loss_source = researchbitcoin_metric(
            token, "supply_in_loss", "d1"
        )
        loss_percent, _ = researchbitcoin_metric(
            token, "supply_in_loss_percent", "d1"
        )
    except Exception as e:
        st.warning(f"Chưa lấy được Supply in Loss trực tiếp: {e}")
else:
    st.info("Không có token ResearchBitcoin: chỉ số Supply in Loss sẽ để N/A.")

if urpd is not None:
    urpd = urpd.copy()
    urpd["mid_price"] = (urpd.price_low + urpd.price_high) / 2
    total_urpd = float(urpd.btc_amount.sum())
    # Chỉ số chính: nguồn cung có giá vốn nằm từ giá BTC hiện tại đến ATH.
    # Có xử lý phần bucket cắt ngang giá hiện tại bằng nội suy theo tỷ lệ.
    above_price_btc = btc_in_range(urpd, float(price), float(ath))
    top_btc = above_price_btc  # tương thích với phần lịch sử cũ
    bottom_btc = btc_in_range(urpd, bottom_start, bottom_end)
else:
    total_urpd = above_price_btc = top_btc = bottom_btc = None

today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
if isinstance(urpd_date, str) and len(urpd_date) == 10 and urpd_date[4] == "-":
    data_date = urpd_date
elif history:
    saved_dates = sorted(
        k for k, v in history.items()
        if isinstance(k, str) and len(k) == 10 and isinstance(v, dict) and v.get("urpd")
    )
    data_date = saved_dates[-1] if saved_dates else None
else:
    data_date = None

# -----------------------------------------------------------------------------
# THAY ĐỔI CẤU TRÚC ĐÃ LỌC GIÁ (FIXED-PRICE STRUCTURAL DELTA)
# -----------------------------------------------------------------------------
# Không so sánh "giá hôm nay -> ATH" với "giá hôm qua -> ATH", vì cách đó
# trộn thay đổi URPD với thay đổi của chính giá BTC. Thay vào đó, dùng CÙNG
# một mức giá tham chiếu của snapshot đang xem cho cả hai ngày.
def _top_at_reference_price(snapshot_df, reference_price, ath_value):
    if snapshot_df is None or snapshot_df.empty:
        return None
    try:
        return btc_in_range(snapshot_df, float(reference_price), float(ath_value))
    except Exception:
        return None


def _structural_top_delta_for_date(target_date, reference_price, days=1):
    """
    Thay đổi cung hiện tại -> ATH sau khi loại ảnh hưởng do giá di chuyển.
    Cả snapshot mới và snapshot cũ đều được cắt tại cùng reference_price.
    """
    if not target_date or not history:
        return None, None
    try:
        d0 = datetime.strptime(target_date, "%Y-%m-%d").date()
        old_date = (d0 - timedelta(days=days)).strftime("%Y-%m-%d")
        new_saved = history.get(target_date, {})
        old_saved = history.get(old_date, {})
        if not new_saved.get("urpd") or not old_saved.get("urpd"):
            return None, old_date
        new_df = normalize_urpd(pd.DataFrame(new_saved["urpd"]))
        old_df = normalize_urpd(pd.DataFrame(old_saved["urpd"]))
        new_value = _top_at_reference_price(new_df, reference_price, ath)
        old_value = _top_at_reference_price(old_df, reference_price, ath)
        if new_value is None or old_value is None:
            return None, old_date
        return float(new_value - old_value), old_date
    except Exception:
        return None, None


def _structural_daily_median3(target_date, reference_price):
    """
    Median của 3 thay đổi cấu trúc theo ngày, tất cả đều dùng cùng một
    reference_price. Median giúp giảm ảnh hưởng của một ngày bất thường.
    """
    try:
        d0 = datetime.strptime(target_date, "%Y-%m-%d").date()
    except Exception:
        return None

    daily = []
    for offset in range(1, 4):
        new_date = (d0 - timedelta(days=offset - 1)).strftime("%Y-%m-%d")
        old_date = (d0 - timedelta(days=offset)).strftime("%Y-%m-%d")
        new_saved = history.get(new_date, {})
        old_saved = history.get(old_date, {})
        if not new_saved.get("urpd") or not old_saved.get("urpd"):
            continue
        try:
            new_df = normalize_urpd(pd.DataFrame(new_saved["urpd"]))
            old_df = normalize_urpd(pd.DataFrame(old_saved["urpd"]))
            new_value = _top_at_reference_price(new_df, reference_price, ath)
            old_value = _top_at_reference_price(old_df, reference_price, ath)
            if new_value is not None and old_value is not None:
                daily.append(float(new_value - old_value))
        except Exception:
            continue

    return float(np.median(daily)) if daily else None


previous_dates = sorted(k for k in history if isinstance(k, str) and k < data_date) if data_date else []
previous_above_price_btc = None
if previous_dates:
    # Chỉ dùng để hiển thị tham khảo; Δ chính bên dưới đã được lọc theo giá cố định.
    prev_saved = history.get(previous_dates[-1], {})
    if isinstance(prev_saved, dict) and prev_saved.get("urpd"):
        try:
            prev_df = normalize_urpd(pd.DataFrame(prev_saved["urpd"]))
            previous_above_price_btc = _top_at_reference_price(prev_df, float(price), float(ath))
        except Exception:
            previous_above_price_btc = None

# Δ trên thẻ chính = thay đổi cấu trúc, không còn bị pha bởi việc giá BTC
# thay đổi làm dịch chuyển ranh giới dưới.
above_delta, above_delta_date = _structural_top_delta_for_date(
    data_date, float(price), days=1
)
if above_delta is not None and previous_above_price_btc is not None:
    # previous_above_price_btc đã được tính ở cùng reference price.
    above_delta_pct = (above_delta / float(previous_above_price_btc) * 100) if previous_above_price_btc else 0
else:
    above_delta_pct = None

above_median3 = _structural_daily_median3(data_date, float(price)) if data_date else None

ath_discount = (price - ath) / ath * 100 if ath else 0

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Giá BTC hiện tại", f"${price:,.0f}")
c2.metric("Chiết khấu từ ATH", f"{ath_discount:.2f}%")
c3.metric(
    "Cung BTC trong vùng giá hiện tại → ATH",
    f"{above_price_btc:,.0f} BTC" if above_price_btc is not None else "N/A",
    (f"{above_delta:+,.0f} BTC | {above_delta_pct:+.2f}%" if above_delta is not None else "Chưa có lần trước")
)
if total_urpd and above_price_btc is not None:
    median_text = (
        f"Median 3D đã lọc: {above_median3:+,.0f} BTC/ngày • "
        if above_median3 is not None else
        "Median 3D: chưa đủ snapshot • "
    )
    c3.caption(
        f"{above_price_btc / total_urpd * 100:.2f}% tổng URPD • "
        f"${price:,.0f} → ${ath:,.0f} • " + median_text
    )
c4.metric(
    "Tổng BTC theo URPD",
    f"{total_urpd:,.0f} BTC" if total_urpd is not None else "N/A",
)
c5.metric(
    "Cung đang lỗ",
    f"{loss_btc:,.0f} BTC" if loss_btc is not None else "N/A",
    f"{loss_percent:.2f}%"
    if loss_percent is not None
    else "API chưa trả dữ liệu",
)

# =========================
# THỊ TRƯỜNG TỔNG QUAN
# =========================
# BTC Dominance điều chỉnh loại USDT + USDC:
# BTC market cap / (Tổng market cap - USDT market cap - USDC market cap).
# Mục đích là đo tỷ trọng của BTC trong phần vốn hóa rủi ro còn lại,
# thay vì để hai stablecoin lớn làm phình mẫu số.
if market is not None:
    st.markdown("### 🌐 Tổng quan vốn hóa thị trường")
    m1, m2, m3, m4 = st.columns(4)

    total_change = market.get("total_mcap_change_24h")
    m1.metric(
        "Tổng vốn hóa thị trường",
        f"${market['total_mcap'] / 1e12:.2f}T",
        f"{total_change:+.2f}%" if total_change is not None else None,
    )
    btc_change = market.get("btc_mcap_change_24h")
    alt_change = market.get("alt_ex_stables_change_24h")

    m2.metric(
        "Vốn hóa Bitcoin",
        f"${market['btc_mcap'] / 1e12:.2f}T",
        f"{btc_change:+.2f}%" if btc_change is not None else None,
    )
    m3.metric(
        "BTC Dominance (loại USDT + USDC)",
        f"{market['adjusted_btc_dom']:.2f}%" if market.get("adjusted_btc_dom") is not None else "N/A",
    )
    m4.metric(
        "Vốn hóa Altcoin (trừ USDT + USDC)",
        f"${market['alt_ex_stables'] / 1e12:.2f}T",
        f"{alt_change:+.2f}%" if alt_change is not None else None,
    )

    st.caption(
        "Dominance điều chỉnh = Vốn hóa BTC ÷ (Tổng vốn hóa thị trường − USDT − USDC). "
        f"USDT + USDC hiện khoảng ${(market['usdt_mcap'] + market['usdc_mcap']) / 1e9:.1f}B. "
        "Nguồn: CoinGecko; dữ liệu thị trường được làm mới tối đa mỗi 5 phút."
    )
elif market_error:
    st.warning(f"Chưa lấy được dữ liệu vốn hóa thị trường: {market_error}")

st.markdown("---")
st.subheader("Bảng cung BTC từ giá hiện tại đến ATH và cung đang lỗ")

summary = pd.DataFrame(
    {
        "Chỉ số": [
            "Cung BTC trong vùng giá hiện tại → ATH",
            "Thay đổi cấu trúc (cùng giá tham chiếu)",
            "Median thay đổi cấu trúc 3D",
            "Tổng BTC theo URPD",
            "% cung từ giá hiện tại đến ATH",
            "Cung đang lỗ trực tiếp",
            "% cung đang lỗ trực tiếp",
            "Ngày dữ liệu URPD",
            "Giá BTC hiện tại",
            "Ngưỡng vùng đỉnh",
            "Nguồn URPD",
            "Nguồn Supply in Loss",
        ],
        "Giá trị": [
            f"{above_price_btc:,.2f}" if above_price_btc is not None else "N/A",
            (f"{above_delta:+,.2f} BTC ({above_delta_pct:+.2f}%)" if above_delta is not None else "N/A"),
            (f"{above_median3:+,.2f} BTC/ngày" if above_median3 is not None else "N/A"),
            f"{total_urpd:,.2f}" if total_urpd is not None else "N/A",
            f"{above_price_btc / total_urpd * 100:.4f}%"
            if total_urpd and above_price_btc is not None
            else "N/A",
            f"{loss_btc:,.2f}" if loss_btc is not None else "N/A",
            f"{loss_percent:.4f}%" if loss_percent is not None else "N/A",
            urpd_date or "N/A",
            f"${price:,.2f}",
            f"${price:,.0f}–${ath:,.0f}",
            urpd_source,
            loss_source or "Chưa có dữ liệu",
        ],
        "Đơn vị": ["BTC", "BTC", "BTC/ngày", "BTC", "%", "BTC", "%", "", "USD", "USD", "", ""],
    }
)
st.dataframe(summary, hide_index=True, use_container_width=True)
st.download_button(
    "Tải bảng tổng hợp CSV",
    data=summary.to_csv(index=False).encode("utf-8-sig"),
    file_name="btc_urpd_summary.csv",
    mime="text/csv",
)

st.caption(
    "Định nghĩa: 'Cung BTC trong vùng giá hiện tại → ATH' là lượng BTC ước tính "
    "có giá vốn thực hiện (realized price) nằm trong khoảng giá này theo URPD. "
    "Phần bucket bị cắt ở biên giá được nội suy theo tỷ lệ chiều rộng bucket; "
    "Δ trên thẻ dùng cùng giá tham chiếu giữa hai snapshot để loại ảnh hưởng "
    "do giá BTC dịch chuyển; median 3D dùng để giảm nhiễu ngày bất thường. "
    "Đây không đồng nghĩa toàn bộ lượng BTC đó chắc chắn đang 'mắc kẹt'."
)

st.info(
    f"Snapshot URPD mới nhất: {data_date}. Nguồn URPD ưu tiên Bitview; "
    "BGeometrics chỉ dùng dự phòng. Khi xem lịch sử, biểu đồ và báo cáo dùng "
    "đúng snapshot của ngày được chọn. Supply in Loss vẫn lấy trực tiếp từ ResearchBitcoin."
)

# Lịch sử URPD: lưu toàn bộ bucket gốc theo từng ngày.
# File này nằm cùng thư mục với app.py khi chạy local.
def urpd_to_records(df):
    return df[["price_low", "price_high", "btc_amount"]].to_dict("records")

def records_to_urpd(records):
    return normalize_urpd(pd.DataFrame(records))

# Lưu URPD mới lên history; không ghi đè các ngày cũ.
# Khi Bitview bù nhiều ngày, tất cả snapshot mới được lưu trong cùng một commit GitHub.
if urpd is not None and data_date and history_should_save:
    # Nếu pending_history_snapshots có dữ liệu thì dùng chúng.
    # Nếu không (ví dụ nguồn dự phòng BGeometrics), tạo snapshot cho ngày hiện tại.
    if not pending_history_snapshots:
        pending_history_snapshots[data_date] = {
            "date": data_date,
            "price": float(price) if price is not None else None,
            "top_btc": float(top_btc) if top_btc is not None else None,
            "above_price_btc": float(above_price_btc) if above_price_btc is not None else None,
            "bottom_btc": float(bottom_btc) if bottom_btc is not None else None,
            "total_urpd": float(total_urpd) if total_urpd is not None else None,
            "urpd": urpd_to_records(urpd),
            "source": urpd_source,
        }
    else:
        # Các snapshot Bitview đã có giá đóng cửa của chính ngày đó;
        # không thay bằng giá BTC hiện tại.
        pass

    changed_dates = []
    for snap_date, snapshot in sorted(pending_history_snapshots.items()):
        old_snapshot = history.get(snap_date)
        if old_snapshot != snapshot:
            history[snap_date] = snapshot
            changed_dates.append(snap_date)

    if changed_dates:
        # Giữ tối đa 365 ngày gần nhất.
        history = dict(sorted(history.items())[-365:])

        if github_token and github_repo:
            if github_history_save(history, history_sha):
                st.success(
                    "Đã lưu history URPD lên GitHub: "
                    + ", ".join(changed_dates)
                )
                _, history_sha = github_history_get()
        else:
            try:
                github_history_save(history)
            except Exception as e:
                st.warning(f"Không lưu được lịch sử URPD: {e}")

st.markdown("---")
st.subheader("Lịch sử biến động nguồn cung")
st.caption("Chọn mốc lịch sử để xem đúng snapshot URPD của ngày đó. Nếu history chưa có, dashboard sẽ lấy trực tiếp từ Bitview API và cache kết quả. Với 7/30/120/180 ngày, báo cáo bên dưới còn phân tích cả khoảng thời gian.")

# Lấy ngày dữ liệu URPD mới nhất làm mốc. Nếu nguồn chưa có ngày hôm qua,
# dashboard giữ snapshot gần nhất đã lưu và không tạo ngày giả.
base_date = datetime.strptime(data_date, "%Y-%m-%d").date() if data_date else None

# Gắn ngày cụ thể ngay trên nút để tránh nhầm giữa ngày chạy app
# và ngày dữ liệu URPD thực tế.
if base_date:
    choices = {
        f"Hiện tại [{base_date.strftime('%d/%m/%Y')}]": 0,
        f"1 ngày trước [{(base_date - timedelta(days=1)).strftime('%d/%m/%Y')}]": 1,
        f"7 ngày trước [{(base_date - timedelta(days=7)).strftime('%d/%m/%Y')}]": 7,
        f"30 ngày trước [{(base_date - timedelta(days=30)).strftime('%d/%m/%Y')}]": 30,
        f"120 ngày trước [{(base_date - timedelta(days=120)).strftime('%d/%m/%Y')}]": 120,
        f"180 ngày trước [{(base_date - timedelta(days=180)).strftime('%d/%m/%Y')}]": 180,
    }
else:
    choices = {"Chưa có dữ liệu lịch sử": 0}
selected = st.radio("Xem biểu đồ:", list(choices), horizontal=True)

selected_days = choices[selected]
selected_date = (
    (base_date - timedelta(days=selected_days)).strftime("%Y-%m-%d")
    if base_date else None
)

if selected_days == 0:
    chart_urpd = urpd
    chart_date = data_date
    chart_price = price
    chart_top_btc = top_btc
    chart_bottom_btc = bottom_btc
    chart_total_urpd = total_urpd
else:
    saved = history.get(selected_date)

    # V29: mọi mốc lịch sử khác 0 đều ưu tiên lấy TRỰC TIẾP từ Bitview
    # theo đúng ngày được chọn. bitview_urpd() có cache 30 phút nên việc
    # rerun dashboard hoặc đổi qua lại giữa các mốc không spam API.
    # Nếu Bitview tạm lỗi, fallback về history đã lưu để biểu đồ vẫn dùng được.
    try:
        fetched_hist_urpd, fetched_hist_url, fetched_hist_close, fetched_hist_total = bitview_urpd(selected_date)
        chart_urpd = fetched_hist_urpd
        chart_date = selected_date
        chart_price = float(fetched_hist_close) if fetched_hist_close is not None else (saved.get("price") if saved else price)
        chart_top_btc = (
            btc_in_range(chart_urpd, float(chart_price), float(ath))
            if chart_price is not None else None
        )
        chart_bottom_btc = btc_in_range(chart_urpd, float(bottom_start), float(bottom_end))
        chart_total_urpd = float(chart_urpd.btc_amount.sum())

        fetched_snapshot = {
            "date": selected_date,
            "price": float(chart_price) if chart_price is not None else None,
            "top_btc": float(chart_top_btc) if chart_top_btc is not None else None,
            "above_price_btc": float(chart_top_btc) if chart_top_btc is not None else None,
            "bottom_btc": float(chart_bottom_btc) if chart_bottom_btc is not None else None,
            "total_urpd": float(chart_total_urpd),
            "urpd": urpd_to_records(chart_urpd),
            "source": "Bitview historical API",
            "url": fetched_hist_url,
            "bitview_total_supply": float(fetched_hist_total) if fetched_hist_total is not None else None,
        }

        # Cập nhật history trong phiên để báo cáo/so sánh 1D/3D/7D dùng
        # chính snapshot Bitview vừa lấy. Không ghi đè bằng giá hiện tại.
        history[selected_date] = fetched_snapshot
        history = dict(sorted(history.items())[-365:])

        # Best-effort: lưu snapshot lịch sử vào GitHub để lần sau không mất
        # dữ liệu, nhưng request Bitview vẫn là nguồn ưu tiên khi xem lịch sử.
        try:
            if github_token and github_repo:
                latest_data, latest_sha = github_history_get()
                latest_data[selected_date] = fetched_snapshot
                latest_data = dict(sorted(latest_data.items())[-365:])
                github_history_save(latest_data, latest_sha)
                history = latest_data
            else:
                github_history_save(history)
        except Exception as e:
            st.caption(f"Đã lấy được Bitview nhưng chưa lưu GitHub: {e}")

        st.success(
            f"Đang xem snapshot {selected_date} lấy trực tiếp từ Bitview API "
            f"({len(chart_urpd):,} bucket)."
        )
    except Exception as bitview_error:
        if saved and saved.get("urpd"):
            chart_urpd = records_to_urpd(saved["urpd"])
            chart_date = selected_date
            chart_price = saved.get("price") or price
            chart_top_btc = btc_in_range(chart_urpd, float(chart_price), float(ath))
            chart_bottom_btc = btc_in_range(chart_urpd, float(bottom_start), float(bottom_end))
            chart_total_urpd = float(chart_urpd.btc_amount.sum())
            st.warning(
                f"Bitview tạm thời không lấy được {selected_date}; đang dùng history đã lưu. "
                f"Chi tiết: {bitview_error}"
            )
        else:
            chart_urpd = None
            chart_date = selected_date
            chart_price = price
            chart_top_btc = chart_bottom_btc = chart_total_urpd = None
            st.warning(
                f"Bitview chưa trả được URPD cho ngày {selected_date}: {bitview_error}"
            )

# Giữ chart_urpd là snapshot GỐC đầy đủ.
# Bucket giá 0 chỉ được lọc ở biến chart_plot_urpd ngay trước khi vẽ.
# Nhờ vậy report lịch sử, tổng URPD và mọi phép tính metric không mất bucket 0.

# So sánh nhanh với dữ liệu đã lưu.
if selected_days > 0 and chart_urpd is not None and selected_date:
    current_saved = history.get(data_date, {}) if data_date else {}
    old_saved = history.get(selected_date, {})
    if old_saved:
        def hist_diff(key):
            a, b = current_saved.get(key), old_saved.get(key)
            return a - b if a is not None and b is not None else None

        # Top/bottom được tính lại từ URPD gốc để mọi thay đổi ATH/ngưỡng
        # trong sidebar không làm lịch sử bị lệch với snapshot hiện tại.
        try:
            cur_hist_df = records_to_urpd(current_saved["urpd"])
            old_hist_df = records_to_urpd(old_saved["urpd"])
            cur_hist_price = float(current_saved.get("price", price))
            old_hist_price = float(old_saved.get("price", price))
            delta_top = (
                btc_in_range(cur_hist_df, cur_hist_price, float(ath))
                - btc_in_range(old_hist_df, old_hist_price, float(ath))
            )
            delta_bottom = (
                btc_in_range(cur_hist_df, float(bottom_start), float(bottom_end))
                - btc_in_range(old_hist_df, float(bottom_start), float(bottom_end))
            )
        except Exception:
            delta_top = hist_diff("top_btc")
            delta_bottom = hist_diff("bottom_btc")
        delta_total = hist_diff("total_urpd")
        st.write(
            f"Trong kỳ **{selected_date} → {data_date}**, "
            f"tổng URPD thay đổi **{delta_total:+,.2f} BTC**."
            if delta_total is not None else
            "Chưa đủ dữ liệu để tính thay đổi."
        )
        history_df = pd.DataFrame({
            "Nguồn cung": ["Từ vùng đáy", "Từ vùng đỉnh", "Tổng URPD"],
            "Thay đổi BTC": [
                f"{x:+,.2f} BTC" if x is not None else "N/A"
                for x in [delta_bottom, delta_top, delta_total]
            ],
        })
        st.dataframe(history_df, hide_index=True, use_container_width=True)

st.subheader("Phân bố nguồn cung theo bucket URPD")
st.caption("Cột xanh: giá vốn thấp hơn giá BTC hiện tại • Cột đỏ: giá vốn cao hơn giá BTC hiện tại")

# So sánh trực tiếp ngay trên biểu đồ.
# Quan trọng: khi người dùng chọn một mốc lịch sử ở radio phía trên,
# "1 ngày trước" phải được tính từ NGÀY ĐANG XEM (chart_date), không phải
# luôn tính từ ngày mới nhất của app (data_date). Ví dụ đang xem 09/09
# thì 1 ngày trước phải là 08/09.
comparison_options = {
    "Không so sánh": 0,
    "So với 1 ngày trước": 1,
    "So với 3 ngày trước": 3,
    "So với 7 ngày trước": 7,
}
comparison_label = st.selectbox(
    "So sánh trực tiếp trên biểu đồ",
    list(comparison_options),
    index=1,
)
comparison_days = comparison_options[comparison_label]
comparison_urpd = None
comparison_date = None
comparison_price = None

comparison_base_date = chart_date or data_date
if comparison_days and comparison_base_date:
    comparison_date = (
        datetime.strptime(comparison_base_date, "%Y-%m-%d").date()
        - timedelta(days=comparison_days)
    ).strftime("%Y-%m-%d")
    comparison_saved = history.get(comparison_date)
    if comparison_saved and comparison_saved.get("urpd"):
        comparison_urpd = records_to_urpd(comparison_saved["urpd"])
        comparison_price = comparison_saved.get("price")
        st.caption(
            f"Đang chồng dữ liệu {comparison_base_date} với snapshot {comparison_date}. "
            "Cột đang xem giữ màu xanh/đỏ; cột lịch sử màu xám để nhìn chênh lệch ngay từng bucket."
        )
    else:
        st.warning(
            f"Chưa có snapshot URPD cho ngày {comparison_date} để so sánh "
            f"với ngày {comparison_base_date}."
        )

if chart_urpd is None:
    st.warning("Chưa có dữ liệu URPD để vẽ biểu đồ.")
else:
    # Chỉ ẩn bucket giá 0 trên biểu đồ; tuyệt đối không sửa snapshot gốc.
    chart_plot_urpd = chart_urpd[chart_urpd.price_low > 0].copy().reset_index(drop=True)
    chart_plot_urpd["mid_price"] = (chart_plot_urpd.price_low + chart_plot_urpd.price_high) / 2
    urpd_plot = chart_plot_urpd
    price_for_chart = chart_price if chart_price is not None else price
    colors = np.where(urpd_plot.mid_price < price_for_chart, "#10b981", "#ef4444")

    fig = go.Figure()

    # Vẽ snapshot lịch sử trước để snapshot hiện tại nằm nổi lên trên.
    if comparison_urpd is not None:
        # Chỉ ẩn bucket giá 0 trên biểu đồ; không xóa khỏi dữ liệu/tổng URPD.
        comparison_urpd_plot = comparison_urpd[comparison_urpd.price_low > 0].copy()
        hist = comparison_urpd_plot.copy()
        hist["mid_price"] = (hist.price_low + hist.price_high) / 2
        hist_width = (hist.price_high - hist.price_low) * 0.92
        fig.add_trace(
            go.Bar(
                x=hist.mid_price,
                y=hist.btc_amount,
                width=hist_width,
                name=f"{comparison_date}",
                marker_color="rgba(148, 163, 184, 0.28)",
                marker_line=dict(color="rgba(148, 163, 184, 0.75)", width=1),
                customdata=np.stack(
                    [hist.price_low, hist.price_high], axis=-1
                ),
                hovertemplate=(
                    f"Snapshot {comparison_date}<br>"
                    "Khoảng giá: $%{customdata[0]:,.0f} - "
                    "$%{customdata[1]:,.0f}<br>"
                    "BTC: %{y:,.2f}<extra></extra>"
                ),
            )
        )

    # Khi có snapshot so sánh, ghép BTC của cùng bucket giá để hover hiện
    # đồng thời ngày cũ, ngày mới và phần tăng/giảm. Không cần đổi sang
    # radio lịch sử để kiểm tra từng cột.
    if comparison_urpd is not None:
        hist_lookup = {
            (round(float(r.price_low), 6), round(float(r.price_high), 6)): float(r.btc_amount)
            for r in comparison_urpd_plot.itertuples(index=False)
        }
        compare_btc = np.array([
            hist_lookup.get(
                (round(float(lo), 6), round(float(hi), 6)),
                np.nan,
            )
            for lo, hi in zip(urpd_plot.price_low, urpd_plot.price_high)
        ], dtype=float)
        delta_btc = urpd_plot.btc_amount.to_numpy(float) - compare_btc
        current_values = urpd_plot.btc_amount.to_numpy(float)
        delta_pct = np.where(
            np.isfinite(compare_btc) & (compare_btc != 0),
            delta_btc / compare_btc * 100.0,
            np.nan,
        )
        current_customdata = np.column_stack([
            urpd_plot.price_low.to_numpy(float),
            urpd_plot.price_high.to_numpy(float),
            compare_btc,
            delta_btc,
            delta_pct,
        ])
        current_hover = (
            f"Snapshot {chart_date}<br>"
            "Khoảng giá: $%{customdata[0]:,.0f} - $%{customdata[1]:,.0f}<br>"
            "BTC ngày mới: %{y:,.2f}<br>"
            f"BTC ngày {comparison_date}: %{{customdata[2]:,.2f}}<br>"
            "Thay đổi: %{customdata[3]:+,.2f} BTC<br>"
            "Thay đổi %: %{customdata[4]:+.2f}%"
            "<extra></extra>"
        )
    else:
        current_customdata = np.stack(
            [urpd_plot.price_low.to_numpy(float), urpd_plot.price_high.to_numpy(float)], axis=-1
        )
        current_hover = (
            f"Snapshot {chart_date}<br>"
            "Khoảng giá: $%{customdata[0]:,.0f} - $%{customdata[1]:,.0f}<br>"
            "BTC: %{y:,.2f}<extra></extra>"
        )

    fig.add_trace(
        go.Bar(
            x=urpd_plot.mid_price,
            y=urpd_plot.btc_amount,
            width=(urpd_plot.price_high - urpd_plot.price_low) * (0.72 if comparison_urpd is not None else 0.92),
            name=f"Hiện tại {chart_date}",
            marker_color=colors,
            customdata=current_customdata,
            hovertemplate=current_hover,
        )
    )

    # Khi đang so sánh, ghi trực tiếp mức tăng/giảm BTC lên biểu đồ.
    # Chỉ ẩn các thay đổi cực nhỏ để biểu đồ không bị phủ kín nhãn;
    # tooltip vẫn luôn hiển thị đầy đủ số BTC và chênh lệch.
    if comparison_urpd is not None:
        valid_delta = np.isfinite(delta_btc)
        label_mask = valid_delta & (np.abs(delta_btc) >= 50.0)
        delta_text = np.where(
            label_mask,
            [f"{d:+,.0f} BTC" if np.isfinite(d) else "" for d in delta_btc],
            "",
        )
        delta_y = urpd_plot.btc_amount.to_numpy(float) + np.maximum(urpd_plot.btc_amount.to_numpy(float) * 0.012, 150.0)
        fig.add_trace(
            go.Scatter(
                x=urpd_plot.mid_price,
                y=delta_y,
                mode="text",
                text=delta_text,
                textposition="top center",
                textfont=dict(size=11, family="Arial Black"),
                cliponaxis=False,
                hoverinfo="skip",
                showlegend=False,
            )
        )
    # Tô nền vùng đáy theo khoảng giá người dùng thiết lập.
    fig.add_vrect(
        x0=bottom_start,
        x1=bottom_end,
        fillcolor="#2563eb",
        opacity=0.13,
        line_width=0,
        layer="below",
    )

    # Tách riêng nhãn giá hiện tại và ngưỡng vùng đỉnh.
    fig.add_vline(
        x=price_for_chart,
        line_dash="dash",
        line_color="white",
        annotation_text=f"Giá ngày {chart_date}: ${price_for_chart:,.0f}",
        annotation_position="top left",
        annotation_y=1.02,
    )
    fig.add_vline(
        x=top_start,
        line_dash="dot",
        line_color="#f59e0b",
        annotation_text=f"Ngưỡng vùng đỉnh: ${top_start:,.0f}",
        annotation_position="top right",
        annotation_y=0.90,
    )
    # Đặt nhãn vùng đáy thấp xuống trong phần nền xanh,
    # tránh chồng lên nhãn giá hiện tại.
    fig.add_annotation(
        x=(bottom_start + bottom_end) / 2,
        y=0.08,
        yref="paper",
        text=f"Vùng đáy: ${bottom_start:,.0f}–${bottom_end:,.0f}",
        showarrow=False,
        font=dict(color="#93c5fd", size=12),
        bgcolor="rgba(15, 23, 42, 0.78)",
        bordercolor="rgba(96, 165, 250, 0.45)",
        borderwidth=1,
        borderpad=5,
    )
    # Giữ cố định khung trục để khi đổi snapshot, biểu đồ không tự zoom/co giật.
    # Trục X phủ toàn bộ vùng 0 → ATH; trục Y có sàn 1M BTC và chỉ nới khi cần.
    y_candidates = [float(urpd_plot.btc_amount.max()) if not urpd_plot.empty else 0.0]
    if comparison_urpd is not None and not comparison_urpd_plot.empty:
        y_candidates.append(float(comparison_urpd_plot.btc_amount.max()))
    y_axis_max = max(1_000_000.0, max(y_candidates, default=0.0) * 1.15)
    x_axis_max = max(float(ath), float(top_start), float(bottom_end), 1.0) * 1.03

    fig.update_layout(
        template="plotly_dark",
        height=560,
        xaxis=dict(
            title="Giá vốn on-chain ($)",
            range=[0, x_axis_max],
            autorange=False,
        ),
        yaxis=dict(
            title="BTC",
            range=[0, y_axis_max],
            autorange=False,
        ),
        margin=dict(l=55, r=55, t=75, b=55),
        bargap=0.02,
        barmode="overlay",
        showlegend=comparison_urpd is not None,
        legend=dict(orientation="h", y=1.08, x=0),
    )

    # V22: animation thật bằng requestAnimationFrame.
    # Streamlit vẫn rerun ở server khi bấm radio, nhưng iframe nhận figure cũ
    # và figure mới rồi tự nội suy từng frame trên client. Không dùng
    # Plotly.animate vì Plotly có thể bỏ qua tween của bar khi x/y thay đổi
    # đồng thời hoặc khi trace có cấu trúc khác nhau.
    current_fig_json = json.loads(fig.to_json())
    animation_key = f"{chart_date}|{comparison_date}|{comparison_days}"
    previous_fig_json = st.session_state.get("urpd_previous_fig_json")
    previous_animation_key = st.session_state.get("urpd_previous_animation_key")
    should_animate = previous_fig_json is not None and previous_animation_key != animation_key

    old_payload = json.dumps(previous_fig_json, ensure_ascii=False) if should_animate else "null"
    new_payload = json.dumps(current_fig_json, ensure_ascii=False)

    chart_html = f"""
    <div id="urpd-chart-wrap" style="width:100%;height:560px;position:relative;overflow:hidden;background:#0e1117;border-radius:10px;">
      <div id="urpd-chart" style="width:100%;height:100%;"></div>
    </div>
    <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
    <script>
      const oldFig = {old_payload};
      const newFig = {new_payload};
      const el = document.getElementById('urpd-chart');
      const config = {{responsive:true, displaylogo:false, scrollZoom:false}};
      const DURATION = 900;

      function clone(obj) {{
        return JSON.parse(JSON.stringify(obj));
      }}

      function isNum(v) {{
        return typeof v === 'number' && Number.isFinite(v);
      }}

      function lerp(a, b, t) {{
        return a + (b - a) * t;
      }}

      function interpolateArray(a, b, t) {{
        if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return null;
        const out = new Array(a.length);
        for (let i = 0; i < a.length; i++) {{
          if (isNum(a[i]) && isNum(b[i])) out[i] = lerp(a[i], b[i], t);
          else out[i] = t < 1 ? a[i] : b[i];
        }}
        return out;
      }}

      function ease(t) {{
        // smoothstep + cubic easing: mượt đầu/cuối, rõ chuyển động ở giữa
        return t * t * (3 - 2 * t);
      }}

      function makeFrameData(t) {{
        const frame = clone(newFig.data);
        for (let i = 0; i < frame.length; i++) {{
          const o = oldFig.data[i];
          const n = newFig.data[i];
          if (!o || !n) continue;

          // Nội suy trực tiếp x/y/width của bar. Đây là phần làm các cột
          // thật sự di chuyển và cao lên/thấp xuống thay vì chỉ fade.
          for (const key of ['x', 'y', 'width']) {{
            if (o[key] !== undefined && n[key] !== undefined) {{
              const arr = interpolateArray(o[key], n[key], t);
              if (arr) frame[i][key] = arr;
              else if (isNum(o[key]) && isNum(n[key])) frame[i][key] = lerp(o[key], n[key], t);
            }}
          }}

          // Các nhãn delta/text không nên hiện nửa cũ nửa mới; giữ nhãn cũ
          // trong lúc chạy và chuyển sang nhãn mới ở frame cuối.
          if (t < 1 && o.text !== undefined && n.text !== undefined) frame[i].text = o.text;
        }}
        return frame;
      }}

      function renderNew() {{
        return Plotly.newPlot(el, newFig.data, newFig.layout, config);
      }}

      function animateBars() {{
        const start = performance.now();
        function tick(now) {{
          const raw = Math.min(1, (now - start) / DURATION);
          const t = ease(raw);
          const frameData = makeFrameData(t);
          Plotly.react(el, frameData, newFig.layout, config);
          if (raw < 1) {{
            requestAnimationFrame(tick);
          }} else {{
            // Chốt chính xác figure mới sau frame cuối.
            Plotly.react(el, newFig.data, newFig.layout, config);
          }}
        }}
        requestAnimationFrame(tick);
      }}

      if (oldFig && Array.isArray(oldFig.data) && Array.isArray(newFig.data) &&
          oldFig.data.length === newFig.data.length &&
          oldFig.data.every((d, i) => d && newFig.data[i] &&
              Array.isArray(d.y) && Array.isArray(newFig.data[i].y) &&
              d.y.length === newFig.data[i].y.length)) {{
        Plotly.newPlot(el, oldFig.data, oldFig.layout, config).then(function() {{
          requestAnimationFrame(animateBars);
        }}).catch(renderNew);
      }} else {{
        // Khi bật/tắt chế độ so sánh làm số trace khác nhau, vẫn dùng
        // cross-fade ngắn; còn chuyển ngày cùng chế độ thì luôn tween thật.
        renderNew();
      }}
    </script>
    """
    components.html(chart_html, height=560, scrolling=False)

    # Chỉ lưu figure sau khi đã chuẩn bị payload để lần rerun kế tiếp có
    # snapshot trước làm điểm bắt đầu cho animation.
    st.session_state["urpd_previous_fig_json"] = current_fig_json
    st.session_state["urpd_previous_animation_key"] = animation_key

    # =========================
    # NEWS / MACRO CONTEXT — dùng làm lớp 40% cho triển vọng cuối cùng.
    # News Radar vẫn độc lập về mặt dữ liệu, nhưng V26 dùng tín hiệu định hướng
    # của tin mới để hợp nhất với điểm URPD theo tỷ trọng 60/40.
    # =========================
    news_rows = fetch_news_radar(days=7, max_items=30)
    upcoming = upcoming_btc_events(days=7)

    macro_score = 50.0
    macro_parts = []
    now_utc = datetime.now(timezone.utc)
    for item in news_rows[:30]:
        impact, direction = news_impact(item["title"], item["category"])
        if direction == "🟡 Chưa rõ":
            continue
        age_h = max(0.0, (now_utc - item["published"]).total_seconds() / 3600.0)
        # Tin mới có trọng số cao hơn; sau 7 ngày trọng số vẫn còn nhưng nhỏ.
        recency_weight = max(0.20, 1.0 - age_h / (7.0 * 24.0))
        impact_weight = 1.35 if impact == "Trực tiếp" else 0.85
        # Giới hạn mỗi headline để một cụm tin lặp lại không chi phối toàn bộ điểm.
        effect = 5.0 * recency_weight * impact_weight
        if direction.startswith("🔴"):
            effect = -effect
        macro_score += effect
        macro_parts.append((item, impact, direction, effect))

    macro_score = float(np.clip(macro_score, 0.0, 100.0))
    if macro_score >= 55:
        macro_bias = "🟢 Macro nghiêng thuận lợi"
    elif macro_score <= 45:
        macro_bias = "🔴 Macro nghiêng bất lợi"
    else:
        macro_bias = "🟡 Macro trung tính / trái chiều"

    # =========================
    # BÁO CÁO TỰ ĐỘNG NGÀY
    # =========================
    # Báo cáo luôn bám đúng NGÀY ĐANG XEM trên radio lịch sử.
    # Các mốc 1D / 3D / 7D cũng tính lùi từ chính ngày đó.
    # Không dùng tin tức bên ngoài. Kết luận chỉ là tín hiệu on-chain,
    # không phải dự báo chắc chắn về giá.
    report_date = chart_date or data_date
    report_urpd = chart_urpd if chart_urpd is not None else urpd
    if report_date and report_urpd is not None:
        st.subheader(f"📊 Báo cáo phân tích URPD ngày {report_date}")
        st.caption(
            f"Phân tích đúng snapshot {report_date}; các mốc 1D / 3D / 7D "
            "đều được tính lùi từ chính ngày này."
        )

        def _fmt_btc(x):
            return f"{x:+,.0f} BTC"

        def _pct(x):
            return f"{x:+.2f}%"

        saved_report = history.get(report_date, {}) if history else {}
        report_price_saved = saved_report.get("price") if isinstance(saved_report, dict) else None
        current_price = float(
            chart_price if chart_date == report_date and chart_price is not None
            else (report_price_saved if report_price_saved is not None else price)
        )

        # =========================================================
        # V30 — BÁO CÁO XU HƯỚNG THEO CẢ KHOẢNG THỜI GIAN
        # ---------------------------------------------------------
        # Biểu đồ vẫn là snapshot đúng report_date. Nếu chọn 7/30/120/180D,
        # phần này phân tích cả khoảng từ report_date đến ngày trước base_date.
        # =========================================================
        trend_df = pd.DataFrame()
        trend_frames = {}
        trend_period_start = None
        trend_period_end = None
        trend_score = None
        trend_bias = None
        trend_price_pct = None
        trend_overhead_change = None
        trend_support_change = None
        trend_resistance_change = None
        trend_bottom_change = None
        trend_total_change = None
        if selected_days >= 7 and base_date:
            trend_period_start = report_date
            trend_period_end = (base_date - timedelta(days=1)).strftime("%Y-%m-%d")
            try:
                trend_df, trend_frames = bitview_period_snapshots(
                    trend_period_start, trend_period_end, selected_days,
                    float(ath), float(bottom_start), float(bottom_end)
                )
            except Exception:
                trend_df, trend_frames = pd.DataFrame(), {}

            if len(trend_df) >= 2:
                first = trend_df.iloc[0]
                last = trend_df.iloc[-1]
                trend_price_change = float(last["price"] - first["price"])
                trend_price_pct = trend_price_change / float(first["price"]) * 100.0 if first["price"] else np.nan
                trend_overhead_change = float(last["overhead_ath"] - first["overhead_ath"])
                trend_support_change = float(last["support_near"] - first["support_near"])
                trend_resistance_change = float(last["resistance_near"] - first["resistance_near"])
                trend_bottom_change = float(last["bottom_supply"] - first["bottom_supply"])
                trend_total_change = float(last["total_urpd"] - first["total_urpd"])

                trend_score = 50.0
                e1 = float(np.clip((-trend_overhead_change / 150000.0) * 18.0, -18.0, 18.0))
                e2 = float(np.clip((trend_support_change / 150000.0) * 10.0, -10.0, 10.0))
                e3 = float(np.clip((-trend_resistance_change / 150000.0) * 8.0, -8.0, 8.0))
                e4 = float(np.clip((trend_price_pct / 10.0) * 8.0, -8.0, 8.0)) if np.isfinite(trend_price_pct) else 0.0
                trend_score = float(np.clip(trend_score + e1 + e2 + e3 + e4, 0.0, 100.0))

                if trend_score >= 65:
                    trend_bias = "🟢 Cấu trúc dài kỳ thuận lợi"
                elif trend_score >= 55:
                    trend_bias = "🟢 Nghiêng tích cực"
                elif trend_score >= 45:
                    trend_bias = "🟡 Cân bằng / chưa rõ"
                elif trend_score >= 35:
                    trend_bias = "🟠 Nghiêng tiêu cực"
                else:
                    trend_bias = "🔴 Cấu trúc dài kỳ bất lợi"

                # Đếm số bước tăng/giảm để tránh kết luận chỉ từ đầu-cuối.
                diffs = trend_df[["price", "overhead_ath"]].diff().dropna()
                price_up_steps = int((diffs["price"] > 0).sum())
                price_down_steps = int((diffs["price"] < 0).sum())
                overhead_down_steps = int((diffs["overhead_ath"] < 0).sum())
                overhead_up_steps = int((diffs["overhead_ath"] > 0).sum())

                # Lưu ra biến dùng cho phần báo cáo hiển thị bên dưới.
                trend_first = first
                trend_last = last
                trend_price_change = trend_price_change
                trend_sr_start = float(first["sr_ratio"]) if pd.notna(first["sr_ratio"]) else np.nan
                trend_sr_end = float(last["sr_ratio"]) if pd.notna(last["sr_ratio"]) else np.nan

                st.markdown(
                    f"### 📈 Báo cáo xu hướng {selected_days} ngày — {trend_period_start} → {trend_period_end}"
                )
                sampling_note = (
                    "Đã lấy đủ từng snapshot trong khoảng này."
                    if selected_days <= 30 else
                    "Giai đoạn >30 ngày dùng mốc đầu/cuối và mỗi 7 ngày để giảm số lượt gọi API; "
                    "đây là xu hướng theo snapshot đại diện, không phải trung bình của mọi ngày."
                )
                st.caption(
                    f"Có {len(trend_df)} snapshot trong khoảng {selected_days} ngày. {sampling_note}"
                )

                t1, t2, t3, t4 = st.columns(4)
                with t1:
                    st.metric(
                        "Giá đầu → cuối",
                        f"${trend_first['price']:,.0f} → ${trend_last['price']:,.0f}",
                        f"{trend_price_change:+,.0f} USD ({trend_price_pct:+.2f}%)",
                    )
                with t2:
                    st.metric("Cung → ATH", f"{trend_last['overhead_ath']:,.0f} BTC", f"{trend_overhead_change:+,.0f} BTC")
                with t3:
                    st.metric("Hỗ trợ gần", f"{trend_last['support_near']:,.0f} BTC", f"{trend_support_change:+,.0f} BTC")
                with t4:
                    st.metric("Cản gần", f"{trend_last['resistance_near']:,.0f} BTC", f"{trend_resistance_change:+,.0f} BTC")

                st.write(f"**🧠 Đánh giá xu hướng:** {trend_bias} — điểm cấu trúc {trend_score:.0f}/100.")
                st.write(
                    f"**🔎 Diễn biến:** giá tăng {price_up_steps} bước / giảm {price_down_steps} bước; "
                    f"cung → ATH giảm {overhead_down_steps} bước / tăng {overhead_up_steps} bước."
                )

                trend_rows = [
                    ["Giá BTC", f"${trend_first['price']:,.0f}", f"${trend_last['price']:,.0f}", f"{trend_price_change:+,.0f} USD ({trend_price_pct:+.2f}%)"],
                    ["Cung dưới giá", f"{trend_first['below_price']:,.0f} BTC", f"{trend_last['below_price']:,.0f} BTC", f"{trend_last['below_price']-trend_first['below_price']:+,.0f} BTC"],
                    ["Cung hiện tại → ATH", f"{trend_first['overhead_ath']:,.0f} BTC", f"{trend_last['overhead_ath']:,.0f} BTC", f"{trend_overhead_change:+,.0f} BTC"],
                    ["Cung hỗ trợ gần −10%", f"{trend_first['support_near']:,.0f} BTC", f"{trend_last['support_near']:,.0f} BTC", f"{trend_support_change:+,.0f} BTC"],
                    ["Cung cản gần +10%", f"{trend_first['resistance_near']:,.0f} BTC", f"{trend_last['resistance_near']:,.0f} BTC", f"{trend_resistance_change:+,.0f} BTC"],
                    ["Tỷ lệ hỗ trợ / cản", f"{trend_sr_start:.2f}x" if np.isfinite(trend_sr_start) else "N/A", f"{trend_sr_end:.2f}x" if np.isfinite(trend_sr_end) else "N/A", f"{trend_sr_end-trend_sr_start:+.2f}x" if np.isfinite(trend_sr_start) and np.isfinite(trend_sr_end) else "N/A"],
                    ["Vùng đáy", f"{trend_first['bottom_supply']:,.0f} BTC", f"{trend_last['bottom_supply']:,.0f} BTC", f"{trend_bottom_change:+,.0f} BTC"],
                    ["Tổng URPD", f"{trend_first['total_urpd']:,.0f} BTC", f"{trend_last['total_urpd']:,.0f} BTC", f"{trend_total_change:+,.0f} BTC"],
                ]
                st.dataframe(
                    pd.DataFrame(trend_rows, columns=["Chỉ số", "Đầu kỳ", "Cuối kỳ", "Thay đổi"]),
                    hide_index=True,
                    use_container_width=True,
                )

                peak_overhead = trend_df.loc[trend_df["overhead_ath"].idxmax()]
                low_overhead = trend_df.loc[trend_df["overhead_ath"].idxmin()]
                peak_support = trend_df.loc[trend_df["support_near"].idxmax()]
                best_price = trend_df.loc[trend_df["price"].idxmax()]
                worst_price = trend_df.loc[trend_df["price"].idxmin()]
                st.info(
                    f"**Mốc đáng chú ý:** cung → ATH cao nhất {peak_overhead['overhead_ath']:,.0f} BTC ({peak_overhead['date']}), "
                    f"thấp nhất {low_overhead['overhead_ath']:,.0f} BTC ({low_overhead['date']}); "
                    f"cung hỗ trợ gần cao nhất {peak_support['support_near']:,.0f} BTC ({peak_support['date']}); "
                    f"giá cao nhất ${best_price['price']:,.0f} ({best_price['date']}), thấp nhất ${worst_price['price']:,.0f} ({worst_price['date']})."
                )

                with st.expander("🧠 Cách đọc báo cáo xu hướng", expanded=False):
                    st.write(
                        "Biểu đồ URPD vẫn là snapshot đúng ngày được chọn; báo cáo này mới là phần nhìn cả giai đoạn. "
                        "Cung → ATH giảm và cung cản gần giảm thường thuận lợi hơn; cung hỗ trợ gần tăng có thể tạo nền dày hơn "
                        "nhưng không phải bằng chứng chắc chắn của tích lũy. Giá được dùng để xác nhận phản ứng, không thay thế dữ liệu cung."
                    )
                    st.write(
                        f"Điểm xu hướng = 50 cơ sở + tác động của cung → ATH ({e1:+.1f}) + hỗ trợ gần ({e2:+.1f}) "
                        f"+ cản gần ({e3:+.1f}) + phản ứng giá ({e4:+.1f}); điểm này là chỉ số nội bộ, không phải xác suất."
                    )

        def _snapshot_delta(key, days):
            # Riêng "above_price_btc" phải dùng cùng giá tham chiếu của
            # snapshot đang xem để loại nhiễu do giá BTC dịch chuyển.
            if key == "above_price_btc":
                return _structural_top_delta_for_date(report_date, current_price, days=days)
            try:
                d0 = datetime.strptime(report_date, "%Y-%m-%d").date()
                old_date = (d0 - timedelta(days=days)).strftime("%Y-%m-%d")
                old = history.get(old_date, {})
                new = history.get(report_date, {})
                a, b = new.get(key), old.get(key)
                if a is None or b is None:
                    return None, old_date
                return float(a) - float(b), old_date
            except Exception:
                return None, None

        def _bucket_deltas(old_date):
            """
            So sánh bucket của report_date với snapshot cũ.
            Không yêu cầu biên bucket hai ngày giống hệt nhau: lượng BTC của
            bucket cũ được phân bổ theo phần giao nhau về giá (giả định phân bố
            đều trong bucket). Điều này làm so sánh an toàn hơn khi một ngày
            đến từ BGeometrics và ngày kia từ Bitview hoặc khi biên bucket khác nhau.
            """
            cur_df = report_urpd.copy() if report_urpd is not None else None
            old = history.get(old_date, {}) if old_date else {}
            if cur_df is None or cur_df.empty or not old.get("urpd"):
                return None

            old_df = records_to_urpd(old["urpd"])
            old_df = old_df[old_df.price_high > old_df.price_low].copy()
            if old_df.empty:
                return None

            old_lo = old_df.price_low.to_numpy(float)
            old_hi = old_df.price_high.to_numpy(float)
            old_amt = old_df.btc_amount.to_numpy(float)
            old_width = old_hi - old_lo

            rows = []
            for r in cur_df.itertuples(index=False):
                lo = float(r.price_low)
                hi = float(r.price_high)
                if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
                    continue
                # Bucket 0 được giữ trong dữ liệu nhưng không đưa vào phân tích
                # thay đổi trực quan vì đây là phần supply đặc biệt/static.
                if hi <= 1:
                    continue

                overlap = np.maximum(0.0, np.minimum(old_hi, hi) - np.maximum(old_lo, lo))
                old_btc = float(np.sum(old_amt * overlap / old_width))
                cur = float(r.btc_amount)
                if not np.isfinite(cur) or not np.isfinite(old_btc):
                    continue
                delta = cur - old_btc
                mid = (lo + hi) / 2.0
                rows.append({
                    "low": lo,
                    "high": hi,
                    "mid": mid,
                    "current": cur,
                    "old": old_btc,
                    "delta": delta,
                    "pct": (delta / old_btc * 100.0) if old_btc else np.nan,
                })

            return pd.DataFrame(rows) if rows else None

        # Các thay đổi cấp vùng.
        d1_top, d1_date = _snapshot_delta("above_price_btc", 1)
        d3_top, d3_date = _snapshot_delta("above_price_btc", 3)
        d7_top, d7_date = _snapshot_delta("above_price_btc", 7)
        top_median3 = _structural_daily_median3(report_date, current_price)
        d1_bottom, _ = _snapshot_delta("bottom_btc", 1)
        d3_bottom, _ = _snapshot_delta("bottom_btc", 3)
        d7_bottom, _ = _snapshot_delta("bottom_btc", 7)
        d1_total, _ = _snapshot_delta("total_urpd", 1)
        d3_total, _ = _snapshot_delta("total_urpd", 3)
        d7_total, _ = _snapshot_delta("total_urpd", 7)

        total_now = float(report_urpd.btc_amount.sum())
        below_now = float(report_urpd.loc[report_urpd.price_high <= current_price, "btc_amount"].sum())
        overhead_now = btc_in_range(report_urpd, current_price, float(ath))
        above_ath_now = float(report_urpd.loc[report_urpd.price_low >= float(ath), "btc_amount"].sum())
        below_pct = below_now / total_now * 100.0 if total_now else np.nan
        overhead_pct = overhead_now / total_now * 100.0 if total_now else np.nan
        report_bottom_btc = btc_in_range(report_urpd, float(bottom_start), float(bottom_end))

        # =========================================================
        # CUNG - CẦU SUY LUẬN TỪ URPD
        # ---------------------------------------------------------
        # URPD không đo trực tiếp lệnh mua/bán. Vì vậy dashboard không gọi
        # các vùng BTC dưới/ trên giá là "demand" tuyệt đối. Chúng được
        # dùng như proxy (chỉ báo đại diện): cung dưới giá = vùng đỡ tiềm
        # năng; cung trên giá = nguồn cung cần được hấp thụ. Kết luận cuối
        # phải kết hợp thêm phản ứng của giá và hướng dịch chuyển cung.
        # =========================================================
        near_pct = 0.10
        near_low = max(0.0, current_price * (1.0 - near_pct))
        near_high = current_price * (1.0 + near_pct)
        far_low = max(0.0, current_price * 0.80)

        support_near = btc_in_range(report_urpd, near_low, current_price)
        resistance_near = btc_in_range(report_urpd, current_price, near_high)
        support_broad = btc_in_range(report_urpd, far_low, current_price)
        resistance_broad = btc_in_range(report_urpd, current_price, float(ath))

        near_total = support_near + resistance_near
        near_balance_pct = ((support_near - resistance_near) / near_total * 100.0) if near_total else None
        sr_ratio = (support_near / resistance_near) if resistance_near > 0 else None

        def _price_from_history(target_date):
            rec = history.get(target_date, {}) if target_date else {}
            try:
                return float(rec.get("price")) if rec.get("price") is not None else None
            except Exception:
                return None

        def _price_change(days):
            try:
                d0 = datetime.strptime(report_date, "%Y-%m-%d").date()
                old_date = (d0 - timedelta(days=days)).strftime("%Y-%m-%d")
                old_price = _price_from_history(old_date)
                if old_price is None or old_price <= 0:
                    return None, old_date
                return current_price - old_price, old_date
            except Exception:
                return None, None

        p1, p1_date = _price_change(1)
        p3, p3_date = _price_change(3)
        p7, p7_date = _price_change(7)
        p3_pct = (p3 / _price_from_history(p3_date) * 100.0) if p3 is not None and _price_from_history(p3_date) else None
        p7_pct = (p7 / _price_from_history(p7_date) * 100.0) if p7 is not None and _price_from_history(p7_date) else None

        # Điểm cân bằng cung-cầu suy luận 0-100. Đây là điểm nội bộ của
        # dashboard, không phải xác suất giá tăng/giảm.
        flow_score = 50.0
        flow_parts = []

        if near_balance_pct is not None:
            e_balance = float(np.clip(near_balance_pct / 100.0 * 20.0, -20.0, 20.0))
            flow_score += e_balance
            flow_parts.append(("Cán cân cung gần giá", e_balance))

        if d3_top is not None:
            e_overhead = float(np.clip((-d3_top / 150000.0) * 15.0, -15.0, 15.0))
            flow_score += e_overhead
            flow_parts.append(("Thay đổi cung phía trên 3D", e_overhead))

        if d3_bottom is not None:
            e_support = float(np.clip((d3_bottom / 150000.0) * 10.0, -10.0, 10.0))
            flow_score += e_support
            flow_parts.append(("Thay đổi cung hỗ trợ 3D", e_support))

        if p3_pct is not None:
            # Giá tăng cùng cấu trúc cung tốt hơn là tín hiệu xác nhận; giá
            # giảm mạnh làm giảm điểm nhưng chỉ ở trọng số vừa phải.
            e_price = float(np.clip(p3_pct / 5.0 * 8.0, -8.0, 8.0))
            flow_score += e_price
            flow_parts.append(("Phản ứng giá 3D", e_price))

        flow_score = float(np.clip(flow_score, 0.0, 100.0))

        if flow_score >= 65:
            flow_state = "🟢 Cầu tương đối khỏe"
        elif flow_score >= 55:
            flow_state = "🟢 Cầu nhỉnh hơn"
        elif flow_score >= 45:
            flow_state = "🟡 Cân bằng / chưa rõ"
        elif flow_score >= 35:
            flow_state = "🟠 Cung nhỉnh hơn"
        else:
            flow_state = "🔴 Cung tương đối mạnh"

        # Logic kết luận cung-cầu: ưu tiên sự đồng thuận giữa cấu trúc cung
        # và phản ứng giá; không kết luận chỉ từ một bucket.
        if (near_balance_pct is not None and near_balance_pct > 10 and
                (d3_top is None or d3_top <= 0) and (p3 is None or p3 >= 0)):
            flow_conclusion = "Cấu trúc nghiêng thuận lợi cho bên mua: cung hỗ trợ gần giá lớn hơn cung cản gần giá, trong khi cung phía trên không tăng rõ."
            flow_outlook = "📈 Nghiêng tích cực"
        elif (near_balance_pct is not None and near_balance_pct < -10 and
              (d3_top is None or d3_top >= 0) and (p3 is None or p3 <= 0)):
            flow_conclusion = "Cấu trúc nghiêng về phía cung: vùng cản gần giá dày hơn vùng hỗ trợ và/hoặc cung phía trên đang tăng trong khi giá yếu."
            flow_outlook = "📉 Nghiêng tiêu cực"
        elif (p3 is not None and p3 > 0 and d3_top is not None and d3_top > 0):
            flow_conclusion = "Giá vẫn tăng nhưng cung phía trên cũng tăng: đây là trạng thái có thể đang hấp thụ cung, chưa nên xem là breakout đã được xác nhận."
            flow_outlook = "🟡 Tăng nhưng đang hấp thụ cung"
        elif (p3 is not None and p3 < 0 and d3_top is not None and d3_top < 0):
            flow_conclusion = "Giá giảm nhưng cung phía trên cũng giảm: áp lực cản đang nhẹ đi, cần xem cung có chuyển xuống vùng hỗ trợ hay không trước khi kết luận xấu."
            flow_outlook = "🟡 Giảm nhưng cung cản đang nhẹ"
        else:
            flow_conclusion = "Cung và phản ứng giá chưa đồng thuận đủ mạnh để xác định bên mua hay bên bán đang chiếm ưu thế."
            flow_outlook = "➡️ Chưa xác nhận"

        df1 = _bucket_deltas(d1_date)
        df3 = _bucket_deltas(d3_date)
        df7 = _bucket_deltas(d7_date)

        def _top_rows(df, positive=True, n=3):
            if df is None or df.empty:
                return []
            x = df[df.delta > 0] if positive else df[df.delta < 0]
            x = x.sort_values("delta", ascending=not positive).head(n)
            return x.to_dict("records")

        inc1 = _top_rows(df1, True)
        dec1 = _top_rows(df1, False)
        inc3 = _top_rows(df3, True)
        dec3 = _top_rows(df3, False)
        inc7 = _top_rows(df7, True)
        dec7 = _top_rows(df7, False)

        # ---------------------------------------------------------
        # CHẤM ĐIỂM SỨC MẠNH BTC: 0–100
        # Đây là scoring định lượng của dashboard, không phải mô hình dự báo giá.
        # Điểm cơ sở = 50. Các tín hiệu 1D/7D được giới hạn để một biến động đơn lẻ
        # không thể tự mình kéo điểm sang cực đoan.
        # ---------------------------------------------------------
        score = 50.0
        score_parts = []

        def _bounded_effect(delta, favorable_negative=True, scale=100000.0, weight=12.0):
            if delta is None:
                return 0.0
            direction = -1.0 if favorable_negative else 1.0
            raw = direction * float(delta) / scale * weight
            return float(np.clip(raw, -weight, weight))

        # Cung phía trên giá: giảm thường thuận lợi hơn cho việc đi lên,
        # tăng thường tạo thêm nguồn cung cần hấp thụ.
        e_top_1 = _bounded_effect(d1_top, favorable_negative=True, scale=100000.0, weight=15.0)
        score += e_top_1
        if d1_top is not None:
            score_parts.append(("Cung hiện tại → ATH (1D)", e_top_1))

        e_top_3 = _bounded_effect(d3_top, favorable_negative=True, scale=150000.0, weight=9.0)
        score += e_top_3
        if d3_top is not None:
            score_parts.append(("Cung hiện tại → ATH (3D)", e_top_3))

        e_top_7 = _bounded_effect(d7_top, favorable_negative=True, scale=200000.0, weight=10.0)
        score += e_top_7
        if d7_top is not None:
            score_parts.append(("Cung hiện tại → ATH (7D)", e_top_7))

        # Vùng đáy: tăng nguồn cung trong vùng giá thấp hơn có thể tạo nền cung,
        # nhưng không coi đó là bằng chứng chắc chắn của tích lũy.
        e_bottom_1 = _bounded_effect(d1_bottom, favorable_negative=False, scale=100000.0, weight=8.0)
        score += e_bottom_1
        if d1_bottom is not None:
            score_parts.append(("Vùng đáy (1D)", e_bottom_1))

        e_bottom_3 = _bounded_effect(d3_bottom, favorable_negative=False, scale=150000.0, weight=5.0)
        score += e_bottom_3
        if d3_bottom is not None:
            score_parts.append(("Vùng đáy (3D)", e_bottom_3))

        # Bucket lớn nhất quanh giá hiện tại: ưu tiên tín hiệu khi biến động đủ lớn.
        near_low = current_price * 0.90
        near_high = current_price * 1.10
        near_df = df1[(df1.mid >= near_low) & (df1.mid <= near_high)] if df1 is not None else None
        near_net = float(near_df.delta.sum()) if near_df is not None and not near_df.empty else None
        e_near = _bounded_effect(near_net, favorable_negative=False, scale=80000.0, weight=7.0)
        score += e_near
        if near_net is not None:
            score_parts.append(("Dịch chuyển quanh giá (1D)", e_near))

        near_df3 = df3[(df3.mid >= near_low) & (df3.mid <= near_high)] if df3 is not None else None
        near_net3 = float(near_df3.delta.sum()) if near_df3 is not None and not near_df3.empty else None
        e_near3 = _bounded_effect(near_net3, favorable_negative=False, scale=120000.0, weight=4.0)
        score += e_near3
        if near_net3 is not None:
            score_parts.append(("Dịch chuyển quanh giá (3D)", e_near3))

        # Nếu đã có đủ 7 ngày, thêm một tín hiệu xu hướng dài hơn.
        if d7_top is not None and d7_bottom is not None:
            # Ưu tiên khi cung phía trên giảm và vùng đáy giữ/tăng.
            e_structure_7 = float(np.clip((-d7_top / 250000.0) * 5.0 + (d7_bottom / 250000.0) * 5.0, -5.0, 5.0))
            score += e_structure_7
            score_parts.append(("Cấu trúc 7D", e_structure_7))

        score = float(np.clip(score, 0.0, 100.0))

        # Xác định thiên hướng.
        if score >= 65:
            bias = "🟢 Mạnh / nghiêng tăng"
            bias_text = "Cấu trúc nguồn cung đang nghiêng thuận lợi cho phía tăng. Ưu tiên chờ giá xác nhận và tránh đuổi giá khi biến động đã quá nhanh."
        elif score >= 55:
            bias = "🟢 Hơi mạnh"
            bias_text = "Tín hiệu on-chain nghiêng tích cực nhưng chưa đủ mạnh để gọi là xu hướng tăng rõ ràng."
        elif score >= 45:
            bias = "🟡 Trung tính"
            bias_text = "Nguồn cung chưa tạo ưu thế rõ cho bên tăng hay giảm. Ưu tiên chờ xác nhận tại các vùng giá quan trọng."
        elif score >= 35:
            bias = "🟠 Hơi yếu"
            bias_text = "Cấu trúc nguồn cung đang kém thuận lợi hơn. Nên thận trọng với vị thế mới và theo dõi phản ứng ở vùng hỗ trợ."
        else:
            bias = "🔴 Yếu / nghiêng giảm"
            bias_text = "Cấu trúc nguồn cung đang nghiêng bất lợi cho phía tăng. Ưu tiên phòng thủ và chờ cấu trúc cải thiện trước khi tăng rủi ro."

        # V27: hợp nhất 60% URPD + 40% macro/news.
        # Quan trọng: chỉ cho phép kết luận nghiêng tăng/giảm khi HAI lớp cùng hướng.
        combined_score = float(np.clip(score * 0.60 + macro_score * 0.40, 0.0, 100.0))
        urpd_direction = "up" if score >= 55 else ("down" if score <= 45 else "neutral")
        macro_direction = "up" if macro_score >= 55 else ("down" if macro_score <= 45 else "neutral")
        confirm_top = d3_top if d3_top is not None else d1_top

        if urpd_direction == "up" and macro_direction == "up" and combined_score >= 55:
            outlook = "📈 Nghiêng tăng"
            outlook_detail = (
                "URPD và bối cảnh vĩ mô/tin tức đang cùng nghiêng tích cực. "
                "Ưu tiên kịch bản giá giữ vùng hiện tại và hấp thụ dần cung phía trên; "
                "vẫn cần phản ứng giá để xác nhận."
            )
        elif urpd_direction == "down" and macro_direction == "down" and combined_score <= 45:
            outlook = "📉 Nghiêng giảm"
            outlook_detail = (
                "URPD và bối cảnh vĩ mô/tin tức đang cùng nghiêng bất lợi. "
                "Rủi ro chính là giá không hấp thụ được cung phía trên và quay lại kiểm tra hỗ trợ; "
                "vẫn cần phản ứng giá để xác nhận."
            )
        elif urpd_direction == "up" and macro_direction == "down":
            outlook = "➡️ Chưa xác nhận"
            outlook_detail = "URPD nghiêng tích cực nhưng macro/tin tức nghiêng bất lợi; hai lớp tín hiệu đang triệt tiêu nhau."
        elif urpd_direction == "down" and macro_direction == "up":
            outlook = "➡️ Chưa xác nhận"
            outlook_detail = "URPD nghiêng bất lợi nhưng macro/tin tức nghiêng tích cực; hai lớp tín hiệu đang triệt tiêu nhau."
        else:
            outlook = "➡️ Chưa xác nhận"
            outlook_detail = "Một trong hai lớp chưa đủ rõ hoặc hai lớp chưa đồng thuận; không ép kết luận tăng/giảm."

        # Hành động theo kiểu quản trị rủi ro, không phải lệnh mua/bán bắt buộc.
        if outlook == "📈 Nghiêng tăng":
            action = "Có thể ưu tiên giữ/giải ngân từng phần theo kế hoạch, nhưng chờ giá xác nhận vùng cung phía trên và không FOMO."
        elif outlook == "📉 Nghiêng giảm":
            action = "Ưu tiên phòng thủ, giảm đòn bẩy/rủi ro và chờ cấu trúc cung + bối cảnh macro cải thiện; không bắt đáy chỉ dựa trên URPD."
        else:
            action = "Ưu tiên đứng ngoài hoặc giữ tỷ trọng vừa phải; chờ URPD và macro cùng xác nhận thay vì đuổi theo một tín hiệu đơn lẻ."

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.metric("Sức mạnh BTC", f"{combined_score:.0f}/100", f"URPD 60% · Macro 40%")
        with c2:
            st.metric("Cung dưới giá hiện tại", f"{below_now:,.0f} BTC", f"{below_pct:.2f}% tổng URPD")
        with c3:
            st.metric("Cung từ giá hiện tại → ATH", f"{overhead_now:,.0f} BTC", f"{overhead_pct:.2f}% tổng URPD")
        with c4:
            st.metric("Triển vọng", outlook)

        st.write("**🧠 Đánh giá sức mạnh:** " + bias_text)
        st.write(f"**🌐 Macro / tin tức:** {macro_score:.0f}/100 — {macro_bias}.")
        st.write(f"**⚖️ Điểm tổng hợp:** {combined_score:.0f}/100 = URPD 60% + Macro/tin tức 40%.")
        st.write("**📈 Xu hướng sắp tới:** " + outlook_detail)
        st.write("**🎯 Nên làm lúc này:** " + action)

        # Lý do chính giúp ông truy ngược điểm số về dữ liệu.
        reason_text = []
        if d1_top is not None:
            reason_text.append(f"cung hiện tại → ATH {_fmt_btc(d1_top)} trong 1 ngày")
        if d1_bottom is not None:
            reason_text.append(f"vùng đáy ${bottom_start:,.0f}–${bottom_end:,.0f} {_fmt_btc(d1_bottom)}")
        if d3_top is not None:
            reason_text.append(f"3 ngày {_fmt_btc(d3_top)} cung phía trên")
        if near_net is not None:
            reason_text.append(f"dịch chuyển quanh giá {_fmt_btc(near_net)}")
        if d7_top is not None:
            reason_text.append(f"7 ngày {_fmt_btc(d7_top)} cung phía trên")
        if top_median3 is not None:
            reason_text.append(f"median 3D đã lọc {_fmt_btc(top_median3)}/ngày")
        if macro_parts:
            macro_pos = sum(1 for x in macro_parts if x[2].startswith("🟢"))
            macro_neg = sum(1 for x in macro_parts if x[2].startswith("🔴"))
            reason_text.append(f"macro/tin tức có hướng rõ: {macro_pos} thuận lợi, {macro_neg} bất lợi")
        if reason_text:
            st.write("**📌 Cơ sở chính:** " + "; ".join(reason_text) + ".")

        report_rows = [
            ["Sức mạnh BTC", f"{combined_score:.0f}/100", outlook, "URPD 60% + Macro/tin tức 40%", ""],
            ["Macro / tin tức", f"{macro_score:.0f}/100", macro_bias, f"{len(macro_parts)} headline có hướng rõ", ""],
            ["Triển vọng", outlook, "", outlook_detail, ""],
            ["Cung từ giá hiện tại → ATH", f"{overhead_now:,.2f} BTC", _fmt_btc(d1_top) if d1_top is not None else "N/A", _fmt_btc(d3_top) if d3_top is not None else "N/A", _fmt_btc(d7_top) if d7_top is not None else "N/A"],
            ["Median thay đổi cấu trúc 3D", f"{top_median3:+,.2f} BTC/ngày" if top_median3 is not None else "N/A", "Đã cố định giá tham chiếu", "Loại ảnh hưởng do giá dịch chuyển", "Giảm nhiễu ngày bất thường"],
            [f"Vùng đáy ${bottom_start:,.0f}–${bottom_end:,.0f}", f"{report_bottom_btc:,.2f} BTC" if report_bottom_btc is not None else "N/A", _fmt_btc(d1_bottom) if d1_bottom is not None else "N/A", _fmt_btc(d3_bottom) if d3_bottom is not None else "N/A", _fmt_btc(d7_bottom) if d7_bottom is not None else "N/A"],
            ["Tổng URPD", f"{total_now:,.2f} BTC", _fmt_btc(d1_total) if d1_total is not None else "N/A", _fmt_btc(d3_total) if d3_total is not None else "N/A", _fmt_btc(d7_total) if d7_total is not None else "N/A"],
        ]
        st.dataframe(
            pd.DataFrame(report_rows, columns=["Chỉ số", "Hôm nay", "Δ 1 ngày", "Δ 3 ngày", "Δ 7 ngày"]),
            hide_index=True,
            use_container_width=True,
        )

        # =========================================================
        # PHÂN TÍCH CUNG - CẦU: lớp suy luận chính của báo cáo.
        # =========================================================
        st.markdown("### ⚖️ Phân tích cung – cầu theo URPD")
        st.caption(
            "Đây là cán cân cung–cầu suy luận từ vị trí nguồn cung URPD và phản ứng giá, "
            "không phải dữ liệu lệnh mua/bán trực tiếp trên sổ lệnh. Cung dưới giá được xem "
            "là vùng hỗ trợ tiềm năng; cung trên giá là lượng cần được hấp thụ."
        )

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.metric("Cung hỗ trợ gần (−10%)", f"{support_near:,.0f} BTC")
        with c2:
            st.metric("Cung cản gần (+10%)", f"{resistance_near:,.0f} BTC")
        with c3:
            if sr_ratio is not None:
                st.metric("Tỷ lệ hỗ trợ / cản", f"{sr_ratio:.2f}x")
            else:
                st.metric("Tỷ lệ hỗ trợ / cản", "N/A")
        with c4:
            st.metric("Cán cân cung gần giá", f"{near_balance_pct:+.1f}%" if near_balance_pct is not None else "N/A")

        st.info(f"**{flow_outlook} — {flow_state}.** {flow_conclusion}")

        price_rows = [
            ["Giá 1D", f"{p1:+,.0f} USD" if p1 is not None else "N/A", p1_date or "N/A"],
            ["Giá 3D", f"{p3:+,.0f} USD ({p3_pct:+.2f}%)" if p3 is not None and p3_pct is not None else "N/A", p3_date or "N/A"],
            ["Giá 7D", f"{p7:+,.0f} USD ({p7_pct:+.2f}%)" if p7 is not None and p7_pct is not None else "N/A", p7_date or "N/A"],
            ["Cung phía trên → ATH", _fmt_btc(d3_top) if d3_top is not None else "N/A", "Giảm = cung cản nhẹ đi"],
            ["Cung vùng đáy", _fmt_btc(d3_bottom) if d3_bottom is not None else "N/A", "Tăng = nền cung dày hơn"],
        ]
        st.dataframe(
            pd.DataFrame(price_rows, columns=["Tín hiệu", "Thay đổi", "Mốc / cách đọc"]),
            hide_index=True,
            use_container_width=True,
        )

        with st.expander("🧠 Vì sao báo cáo kết luận như vậy?", expanded=False):
            if flow_parts:
                st.write("**Các thành phần của điểm cân bằng cung–cầu:**")
                for name, effect in flow_parts:
                    st.write(f"• {name}: **{effect:+.1f} điểm**")
            st.write(
                "**Cách suy luận:** (1) so sánh cung hỗ trợ và cung cản gần giá; "
                "(2) xem cung phía trên đang tăng hay giảm trong 3 ngày; "
                "(3) xem vùng hỗ trợ thay đổi thế nào; (4) đối chiếu với phản ứng giá. "
                "Chỉ khi nhiều tín hiệu cùng hướng mới nâng mức kết luận."
            )
            st.write(
                "**Không được hiểu là:** cung dưới giá = chắc chắn có người mua, hoặc cung trên giá = chắc chắn có người bán. "
                "URPD chỉ cho biết phân bố giá vốn; hành vi mua/bán cần dữ liệu dòng tiền hoặc sổ lệnh bổ sung."
            )

        def _bucket_text(row):
            return f"${row['low']:,.0f}–${row['high']:,.0f}: {_fmt_btc(row['delta'])} ({_pct(row['pct'])})"

        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown("**📈 Bucket tăng mạnh nhất**")
            if inc1:
                for r in inc1:
                    st.write("• " + _bucket_text(r))
            else:
                st.write("Không có bucket tăng đáng kể / chưa đủ dữ liệu 1 ngày.")
        with col_b:
            st.markdown("**📉 Bucket giảm mạnh nhất**")
            if dec1:
                for r in dec1:
                    st.write("• " + _bucket_text(r))
            else:
                st.write("Không có bucket giảm đáng kể / chưa đủ dữ liệu 1 ngày.")

        with st.expander("🔎 Phân tích 3 ngày & kết luận"):
            if d3_top is None and d3_bottom is None and d3_total is None and not inc3 and not dec3:
                st.warning("Chưa đủ snapshot 3 ngày để đưa ra đánh giá.")
            else:
                # Chấm riêng tín hiệu 3 ngày để người dùng có một kết luận
                # dễ đọc, nhưng không biến nó thành xác suất dự báo.
                score_3d = 50.0

                # Cung hiện tại -> ATH giảm là tín hiệu thuận lợi; tăng là
                # áp lực cung phía trên tăng.
                if d3_top is not None:
                    score_3d += float(np.clip((-d3_top / 150000.0) * 25.0, -25.0, 25.0))

                # Vùng đáy tăng được xem là tín hiệu nền hỗ trợ, nhưng không
                # coi đó là bằng chứng chắc chắn của tích lũy.
                if d3_bottom is not None:
                    score_3d += float(np.clip((d3_bottom / 150000.0) * 12.0, -12.0, 12.0))

                # Dịch chuyển quanh giá: tăng nhẹ nghiêng hỗ trợ, giảm nghiêng yếu.
                if near_net3 is not None:
                    score_3d += float(np.clip((near_net3 / 120000.0) * 10.0, -10.0, 10.0))

                score_3d = float(np.clip(score_3d, 0.0, 100.0))

                if score_3d >= 65:
                    strength_3d = "🟢 Mạnh"
                elif score_3d >= 55:
                    strength_3d = "🟢 Hơi mạnh"
                elif score_3d >= 45:
                    strength_3d = "🟡 Trung tính"
                elif score_3d >= 35:
                    strength_3d = "🟠 Hơi yếu"
                else:
                    strength_3d = "🔴 Yếu"

                # Xác nhận xu hướng bằng cả điểm 3D và hướng cung phía trên.
                if score_3d >= 65 and (d3_top is None or d3_top < 0):
                    trend_3d = "📈 Nghiêng tăng"
                    trend_detail_3d = "Nguồn cung phía trên giảm trong 3 ngày, cho thấy áp lực cản phía trên đang nhẹ đi."
                elif score_3d <= 35 and (d3_top is None or d3_top > 0):
                    trend_3d = "📉 Nghiêng giảm"
                    trend_detail_3d = "Nguồn cung phía trên tăng trong 3 ngày, cho thấy áp lực cản phía trên đang dày lên."
                else:
                    trend_3d = "➡️ Chưa xác nhận"
                    trend_detail_3d = "Các tín hiệu 3 ngày chưa đồng thuận đủ mạnh để xác nhận một hướng rõ ràng."

                # Hành động và rủi ro được suy ra từ cùng bộ tín hiệu 3D.
                if score_3d >= 65:
                    action_3d = "Có thể ưu tiên giữ vị thế hoặc giải ngân từng phần nếu kế hoạch của ông cho phép; tránh FOMO khi giá tăng nhanh."
                    caution_3d = "Cẩn thận khi cung phía trên bắt đầu tăng trở lại hoặc giá tăng nhưng nguồn cung vùng hiện tại không tiếp tục được hấp thụ."
                elif score_3d >= 55:
                    action_3d = "Ưu tiên quan sát và vào từng phần nhỏ thay vì đuổi giá; chờ thêm xác nhận từ 1D/7D."
                    caution_3d = "Cẩn thận với tín hiệu tăng ngắn hạn nhưng 3D chưa đủ mạnh; tránh dùng đòn bẩy cao."
                elif score_3d >= 45:
                    action_3d = "Nên đứng ngoài hoặc giữ quy mô vừa phải, chờ thêm 1–3 snapshot để xác nhận hướng."
                    caution_3d = "Cẩn thận với việc diễn giải một bucket tăng/giảm đơn lẻ thành mua, bán hoặc tích lũy/phân phối."
                elif score_3d >= 35:
                    action_3d = "Ưu tiên giảm rủi ro và hạn chế đòn bẩy; chờ nguồn cung phía trên ổn định lại."
                    caution_3d = "Cẩn thận khi giá không hấp thụ được cung phía trên và quay xuống kiểm tra vùng hỗ trợ."
                else:
                    action_3d = "Ưu tiên phòng thủ, hạn chế mở vị thế lớn và không bắt đáy chỉ dựa vào URPD."
                    caution_3d = "Cẩn thận với áp lực cung phía trên tăng đồng thời vùng hỗ trợ suy yếu."

                st.markdown(f"### 🧭 Kết luận 3 ngày — tính đến {report_date}")
                c1, c2, c3 = st.columns(3)
                with c1:
                    st.metric("Đánh giá sức mạnh", f"{score_3d:.0f}/100")
                    st.write(f"**{strength_3d}**")
                with c2:
                    st.metric("Xu hướng sắp tới", trend_3d)
                with c3:
                    st.write("**Cần cẩn thận**")
                    st.write(caution_3d)

                st.info(f"**Nên làm gì lúc này:** {action_3d}")
                st.caption(trend_detail_3d)

                st.markdown("**📊 Thống kê 3 ngày**")
                if top_median3 is not None:
                    st.caption(f"Median 3D đã lọc: {top_median3:+,.0f} BTC/ngày — dùng để giảm ảnh hưởng của một snapshot bất thường.")
                stat_rows_3d = [
                    ["Cung từ giá snapshot → ATH", _fmt_btc(d3_top) if d3_top is not None else "N/A",
                     "Giảm là thuận lợi; tăng là áp lực cung phía trên tăng"],
                    [f"Vùng đáy ${bottom_start:,.0f}–${bottom_end:,.0f}", _fmt_btc(d3_bottom) if d3_bottom is not None else "N/A",
                     "Tăng có thể hỗ trợ nền giá, nhưng không đồng nghĩa chắc chắn tích lũy"],
                    ["Dịch chuyển quanh giá", _fmt_btc(near_net3) if near_net3 is not None else "N/A",
                     "Tăng nghiêng hỗ trợ; giảm nghiêng yếu"],
                    ["Tổng URPD", _fmt_btc(d3_total) if d3_total is not None else "N/A",
                     "Biến động tổng thể của snapshot, không tự nó cho biết mua/bán"],
                ]
                st.dataframe(
                    pd.DataFrame(stat_rows_3d, columns=["Chỉ số", "Thay đổi 3 ngày", "Cách đọc"]),
                    hide_index=True,
                    use_container_width=True,
                )

                if inc3 or dec3:
                    st.markdown("**Các bucket biến động mạnh trong 3 ngày**")
                    st.info(
                        "**Cách đọc:** 'Tăng mạnh' là các bucket có **mức tăng BTC lớn nhất** so với snapshot cách đây 3 ngày; "
                        "'Giảm mạnh' là các bucket có **mức giảm BTC lớn nhất**. Đây là xếp hạng theo độ lớn thay đổi, "
                        "không có nghĩa chắc chắn là cá voi mua/bán hay tích lũy/phân phối. Một bucket có % thay đổi cao "
                        "nhưng lượng BTC nhỏ vẫn có thể không nằm trong nhóm này nếu mức tăng/giảm BTC tuyệt đối nhỏ hơn. "
                        "Các thay đổi này được tính theo cùng vùng giá và không nên đọc riêng lẻ như tín hiệu giá."
                    )
                    if inc3:
                        st.write("**🟢 Tăng mạnh = BTC trong bucket tăng nhiều nhất:**")
                        for r in inc3:
                            st.write("• " + _bucket_text(r))
                    if dec3:
                        st.write("**🔴 Giảm mạnh = BTC trong bucket giảm nhiều nhất:**")
                        for r in dec3:
                            st.write("• " + _bucket_text(r))

        with st.expander("🌐 Vì sao Macro/Tin tức ảnh hưởng tới Triển vọng?", expanded=True):
            st.write(
                f"**Macro/tin tức: {macro_score:.0f}/100 — {macro_bias}.** "
                "Lớp này chiếm 40% điểm tổng hợp. URPD chiếm 60%. "
                "Dashboard chỉ đổi sang 'Nghiêng tăng/giảm' khi hai lớp cùng hướng; "
                "nếu trái chiều hoặc một lớp chưa rõ thì giữ 'Chưa xác nhận'."
            )
            if macro_parts:
                for item, impact, direction, effect in macro_parts[:5]:
                    st.write(
                        f"• {direction} · {impact} · **{item['title']}** "
                        f"→ {effect:+.1f} điểm macro"
                    )
            else:
                st.write("Chưa có headline đủ rõ hướng để điều chỉnh điểm macro.")
            if upcoming:
                upcoming_high = [e for e in upcoming if "Rất cao" in e["impact"]]
                if upcoming_high:
                    st.info(
                        "Catalyst sắp tới: " + "; ".join(
                            f"{e['date']} {e['event']}" for e in upcoming_high[:3]
                        ) + ". Đây là sự kiện cần theo dõi, không tự cộng điểm tăng/giảm."
                    )

        with st.expander("🔎 Phân tích xu hướng 7 ngày"):
            if inc7 or dec7:
                if inc7:
                    st.write("**Tăng mạnh:**")
                    for r in inc7:
                        st.write("• " + _bucket_text(r))
                if dec7:
                    st.write("**Giảm mạnh:**")
                    for r in dec7:
                        st.write("• " + _bucket_text(r))
            else:
                st.write("Chưa đủ snapshot để phân tích 7 ngày.")

        with st.expander("🧮 Vì sao dashboard cho điểm này?"):
            st.write(f"Điểm URPD cơ sở: **50/100**. Các thay đổi URPD 1D/7D được giới hạn ảnh hưởng để tránh một bucket đơn lẻ chi phối toàn bộ lớp on-chain. Điểm cuối cùng dùng **60% URPD + 40% Macro/tin tức**.")
            if score_parts:
                for name, effect in score_parts:
                    st.write(f"• {name}: **{effect:+.1f} điểm**")
            else:
                st.write("Chưa có snapshot đối chiếu phù hợp để điều chỉnh điểm.")
            st.write("Điểm này chỉ phản ánh **sức mạnh cấu trúc nguồn cung theo URPD**, không phải xác suất BTC tăng/giảm và không thay thế quản trị rủi ro.")

        st.caption(
            "Lưu ý: URPD cho biết phân bố giá vốn của nguồn cung UTXO tại từng snapshot. "
            "Biến động bucket không tự chứng minh mua, bán, tích lũy hay phân phối; báo cáo dùng "
            "các biến động đó để tạo tín hiệu định lượng, đánh giá sức mạnh tương đối và các kịch bản cần theo dõi."
        )

    # Bảng tổng hợp ngay dưới biểu đồ.
    chart_summary = pd.DataFrame({
        "Vùng giá": [
            f"Vùng đáy ${bottom_start:,.0f}–${bottom_end:,.0f}",
            f"Vùng đỉnh từ ${top_start:,.0f}",
            "Giá hiện tại",
        ],
        "BTC": [
            f"{chart_bottom_btc:,.2f}" if chart_bottom_btc is not None else "N/A",
            f"{chart_top_btc:,.2f}" if chart_top_btc is not None else "N/A",
            f"{price_for_chart:,.2f}",
        ],
        "Tỷ trọng URPD": [
            f"{chart_bottom_btc / chart_total_urpd * 100:.4f}%"
            if chart_total_urpd and chart_bottom_btc is not None else "N/A",
            f"{chart_top_btc / chart_total_urpd * 100:.4f}%"
            if chart_total_urpd and chart_top_btc is not None else "N/A",
            "—",
        ],
    })
    st.dataframe(chart_summary, hide_index=True, use_container_width=True)

    with st.expander("Xem URPD gốc"):
        st.dataframe(urpd, hide_index=True, use_container_width=True)

# =========================
# NEWS RADAR — cập nhật độc lập với snapshot URPD
# =========================
st.markdown("---")
st.header("📰 BTC News Radar — Vĩ mô, dòng vốn và sự kiện có thể tác động BTC")
st.caption("Tin tức được làm mới khoảng mỗi 15 phút; lịch sự kiện lọc trong 7 ngày tới. V28 hiển thị tiêu đề tiếng Việt, giữ tiêu đề tiếng Anh gốc và dịch mô tả khi có; News/Macro vẫn chiếm 40% điểm tổng hợp cùng URPD 60%.")

# V27 đã lấy News Radar trước phần báo cáo để dùng được cho điểm tổng hợp.
# Hai biến này được cache 15 phút nên không tạo thêm lượt gọi ngoài ý muốn.

n1, n2 = st.columns([1.35, 1])
with n1:
    st.subheader("🔥 Tin mới nhất / 7 ngày qua")
    st.info("🇻🇳 Tiêu đề tiếng Việt là bản dịch để dễ đọc; 🇬🇧 tiêu đề gốc được giữ lại để đối chiếu. Nếu nguồn không có mô tả hoặc dịch vụ dịch tạm thời lỗi, dashboard sẽ giữ nguyên nội dung gốc.")
    if news_rows:
        for item in news_rows[:15]:
            impact, direction = news_impact(item["title"], item["category"])
            age_h = max(0.0, (datetime.now(timezone.utc) - item["published"]).total_seconds() / 3600.0)
            if age_h < 24:
                age = f"{age_h:.0f} giờ trước"
            else:
                age = f"{age_h/24:.1f} ngày trước"
            title_vi = news_title_vi(item)
            summary_vi = news_summary_vi(item)
            st.markdown(
                f"**🇻🇳 {title_vi}**  \n"
                f"<small>🇬🇧 <i>{item['title']}</i></small>  \n"
                f"`{item['source']}` · `{item['category']}` · `{impact}` · {direction} · `{age}`  "
                f"[Đọc tin gốc]({item['link']})",
                unsafe_allow_html=True,
            )
            if summary_vi:
                st.caption(f"📝 Tóm tắt: {summary_vi}")
            st.markdown("---")
    else:
        st.warning("Chưa lấy được nguồn tin trực tuyến. Dashboard vẫn hoạt động bình thường; thử refresh sau vài phút.")

with n2:
    st.subheader("⏰ 7 ngày sắp tới")
    if upcoming:
        for e in upcoming:
            st.markdown(
                f"**{e['date']} · {e['time']} — {e['event']}**  \n"
                f"{e['impact']} · {e['why']}"
            )
            st.markdown("---")
    else:
        st.info("Không có sự kiện trọng yếu đã cấu hình trong 7 ngày tới.")

# Bảng tóm tắt để báo cáo URPD có thêm bối cảnh vĩ mô.
st.subheader("🧭 Tác động lên BTC — đọc cùng báo cáo URPD")
if 'combined_score' in locals():
    st.info(f"**V27:** Điểm tổng hợp hiện tại **{combined_score:.0f}/100** = URPD 60% + Macro/tin tức 40%. Triển vọng: **{outlook}**.")
macro_flags = []
for item in news_rows[:20]:
    impact, direction = news_impact(item["title"], item["category"])
    if direction != "🟡 Chưa rõ":
        macro_flags.append((item, impact, direction))

if macro_flags:
    cols = st.columns(3)
    for i, (item, impact, direction) in enumerate(macro_flags[:3]):
        with cols[i % 3]:
            st.metric("Tín hiệu tin tức", direction)
            st.caption(f"{impact} · {item['category']}")
            st.write(item["title"])
else:
    st.info("Chưa có đủ tiêu đề rõ hướng để tạo tín hiệu tin tức.")

st.markdown("**Cách dashboard sẽ kết hợp:**")
st.markdown(
    "- 🟢 **URPD hỗ trợ + tin vĩ mô thuận lợi** → mức xác nhận xu hướng tăng cao hơn.  "
    "\n- 🔴 **URPD cản tăng + tin vĩ mô bất lợi** → mức xác nhận xu hướng giảm cao hơn.  "
    "\n- 🟡 **Hai bên trái chiều** → giữ trạng thái *chưa xác nhận*, không ép kết luận.  "
    "\n- ⚠️ Tin tức không được dùng để biến thành 'xác suất BTC tăng/giảm'; nó chỉ là lớp bối cảnh và catalyst cần theo dõi."
)

