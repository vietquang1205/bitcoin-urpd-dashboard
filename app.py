import os
import json
import base64
from datetime import datetime, timedelta, timezone

import requests
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go

st.set_page_config(layout="wide", page_title="BTC URPD Monitor V18", page_icon="🪙")

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
st.caption("URPD thực tế từ Bitview • BGeometrics dự phòng • Supply in Loss trực tiếp • Không mô phỏng")

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
    if refresh_api:
        st.cache_data.clear()
        st.session_state["force_urpd_refresh"] = True

force_refresh = st.session_state.pop("force_urpd_refresh", False)

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

# Nếu không có file, ưu tiên dùng dữ liệu đã lưu. Chỉ gọi API khi:
# 1) Chưa có dữ liệu lịch sử; hoặc 2) người dùng bấm nút cập nhật.
# Khi cập nhật, Bitview sẽ được dùng để bù các ngày còn thiếu kể từ snapshot gần nhất
# (tối đa 7 ngày/lần chạy) để không phải chạy app từng ngày.
history_should_save = False
pending_history_snapshots = {}

if urpd is None:
    saved_dates = sorted(
        k for k, v in history.items()
        if isinstance(k, str) and len(k) == 10 and isinstance(v, dict) and v.get("urpd")
    )
    latest_saved_date = saved_dates[-1] if saved_dates else None

    if latest_saved_date and not force_refresh:
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
            # Không có snapshot mới: giữ dữ liệu cũ, tuyệt đối không tạo ngày giả.
            if latest_saved_date:
                saved_latest = history[latest_saved_date]
                urpd = normalize_urpd(pd.DataFrame(saved_latest["urpd"]))
                urpd_source = saved_latest.get("source", "Lịch sử cục bộ")
                urpd_date = latest_saved_date
                st.warning(
                    f"Bitview chưa trả được snapshot mới. "
                    f"Đang giữ snapshot gần nhất {latest_saved_date}; không tạo snapshot mới."
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

price = btc_price()
if price is None:
    price = st.number_input("Nhập giá BTC hiện tại", value=78818.0)
    price_source = "Giá nhập thủ công"
else:
    price_source = "Giá thị trường trực tiếp"

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
st.caption("Chọn mốc lịch sử để xem lại đúng biểu đồ URPD của ngày đó.")

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
    if saved and saved.get("urpd"):
        chart_urpd = records_to_urpd(saved["urpd"])
        chart_date = selected_date
        chart_price = saved.get("price") or price
        # Tính lại các metric từ snapshot gốc để không phụ thuộc vào ATH
        # hoặc ngưỡng vùng đáy đã dùng khi snapshot được lưu trước đây.
        chart_top_btc = btc_in_range(chart_urpd, float(chart_price), float(ath))
        chart_bottom_btc = btc_in_range(chart_urpd, float(bottom_start), float(bottom_end))
        chart_total_urpd = float(chart_urpd.btc_amount.sum())
        st.success(f"Đang xem URPD gốc của ngày {selected_date}. Không gọi API lại.")
    else:
        chart_urpd = None
        chart_date = selected_date
        chart_price = price
        chart_top_btc = chart_bottom_btc = chart_total_urpd = None
        st.info(
            f"Chưa có dữ liệu URPD gốc cho ngày {selected_date}. "
            "Hãy chạy app vào ngày đó hoặc thêm dữ liệu bằng file URPD."
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
    fig.update_layout(
        template="plotly_dark",
        height=560,
        xaxis_title="Giá vốn on-chain ($)",
        yaxis_title="BTC",
        margin=dict(l=55, r=55, t=75, b=55),
        bargap=0.02,
        barmode="overlay",
        showlegend=comparison_urpd is not None,
        legend=dict(orientation="h", y=1.08, x=0),
    )
    st.plotly_chart(fig, use_container_width=True)

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

        # Xác định xu hướng sắp tới: dựa trên điểm + các điều kiện xác nhận,
        # không dùng ngôn ngữ chắc chắn.
        confirm_top = d3_top if d3_top is not None else d1_top
        if score >= 65 and (confirm_top is None or confirm_top < 0):
            outlook = "📈 Nghiêng tăng"
            outlook_detail = "Kịch bản ưu tiên: giá giữ được vùng hiện tại và hấp thụ dần nguồn cung phía trên; tín hiệu 3D được dùng làm lớp xác nhận."
        elif score <= 35 and (confirm_top is None or confirm_top > 0):
            outlook = "📉 Nghiêng giảm"
            outlook_detail = "Kịch bản rủi ro: giá không hấp thụ được cung phía trên và quay lại kiểm tra các vùng hỗ trợ; tín hiệu 3D được dùng làm lớp xác nhận."
        else:
            outlook = "➡️ Chưa xác nhận"
            outlook_detail = "Cả hai kịch bản vẫn còn mở; cần thêm snapshot và phản ứng giá tại vùng cung gần nhất."

        # Hành động theo kiểu quản trị rủi ro, không phải lệnh mua/bán bắt buộc.
        if score >= 65:
            action = "Ưu tiên giữ vị thế đang có; nếu có kế hoạch giải ngân thì chia nhỏ và chờ giá xác nhận vùng cung phía trên. Không FOMO."
        elif score >= 55:
            action = "Có thể thiên về giữ/giải ngân thận trọng theo kế hoạch, nhưng chờ xác nhận breakout và theo dõi cung phía trên."
        elif score >= 45:
            action = "Ưu tiên đứng ngoài hoặc giữ tỷ trọng vừa phải; chờ thêm 1–3 snapshot để xác định hướng thay vì đuổi theo biến động một ngày."
        elif score >= 35:
            action = "Giảm đòn bẩy/rủi ro nếu đang dùng mức cao; chờ vùng hỗ trợ phản ứng tốt trước khi tăng vị thế."
        else:
            action = "Ưu tiên phòng thủ, hạn chế đòn bẩy và chờ cấu trúc nguồn cung cải thiện; không bắt đáy chỉ dựa trên URPD."

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.metric("Sức mạnh BTC", f"{score:.0f}/100", bias)
        with c2:
            st.metric("Cung dưới giá hiện tại", f"{below_now:,.0f} BTC", f"{below_pct:.2f}% tổng URPD")
        with c3:
            st.metric("Cung từ giá hiện tại → ATH", f"{overhead_now:,.0f} BTC", f"{overhead_pct:.2f}% tổng URPD")
        with c4:
            st.metric("Triển vọng", outlook)

        st.write("**🧠 Đánh giá sức mạnh:** " + bias_text)
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
        if reason_text:
            st.write("**📌 Cơ sở chính:** " + "; ".join(reason_text) + ".")

        report_rows = [
            ["Sức mạnh BTC", f"{score:.0f}/100", bias, "Điểm định lượng của dashboard", ""],
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
                    if inc3:
                        st.write("**Tăng mạnh:**")
                        for r in inc3:
                            st.write("• " + _bucket_text(r))
                    if dec3:
                        st.write("**Giảm mạnh:**")
                        for r in dec3:
                            st.write("• " + _bucket_text(r))

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
            st.write(f"Điểm cơ sở: **50/100**. Điểm được điều chỉnh bởi các thay đổi URPD 1D/7D; mỗi thành phần có giới hạn ảnh hưởng để tránh một bucket đơn lẻ chi phối toàn bộ kết luận.")
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
