import os
import json
import base64
from datetime import datetime, timedelta, timezone

import requests
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go

st.set_page_config(layout="wide", page_title="BTC URPD Monitor", page_icon="🪙")

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
DEFAULT_ATH = 126223.0


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

# So sánh với snapshot URPD gần nhất trước đó.
previous_dates = sorted(k for k in history if isinstance(k, str) and k < data_date)
previous_above_price_btc = None
if previous_dates:
    previous_above_price_btc = history[previous_dates[-1]].get("above_price_btc")
if above_price_btc is not None and previous_above_price_btc is not None:
    above_delta = above_price_btc - float(previous_above_price_btc)
    above_delta_pct = (above_delta / float(previous_above_price_btc) * 100) if previous_above_price_btc else 0
else:
    above_delta = above_delta_pct = None

ath_discount = (price - ath) / ath * 100 if ath else 0

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Giá BTC hiện tại", f"${price:,.0f}")
c2.metric("Chiết khấu từ ATH", f"{ath_discount:.2f}%")
c3.metric(
    "BTC mắc kẹt từ giá hiện tại đến ATH",
    f"{above_price_btc:,.0f} BTC" if above_price_btc is not None else "N/A",
    ((f"{above_delta:+,.0f} BTC | {above_delta_pct:+.2f}%" if above_delta is not None else "Chưa có lần trước"))
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
st.subheader("Bảng nguồn cung từ giá hiện tại đến ATH và cung đang lỗ")

summary = pd.DataFrame(
    {
        "Chỉ số": [
            "BTC mắc kẹt từ giá hiện tại đến ATH",
            "Thay đổi so với snapshot trước",
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
        "Đơn vị": ["BTC", "BTC", "BTC", "%", "BTC", "%", "", "USD", "USD", "", ""],
    }
)
st.dataframe(summary, hide_index=True, use_container_width=True)
st.download_button(
    "Tải bảng tổng hợp CSV",
    data=summary.to_csv(index=False).encode("utf-8-sig"),
    file_name="btc_urpd_summary.csv",
    mime="text/csv",
)

st.info(
    f"Đang sử dụng URPD mới nhất của ngày {data_date}. "
    "Nguồn URPD ưu tiên Bitview; BGeometrics chỉ dùng dự phòng. "
    "Supply in Loss vẫn lấy trực tiếp từ ResearchBitcoin."
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
        chart_top_btc = saved.get("top_btc")
        chart_bottom_btc = saved.get("bottom_btc")
        chart_total_urpd = saved.get("total_urpd")
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

# So sánh nhanh với dữ liệu đã lưu.
if selected_days > 0 and chart_urpd is not None and selected_date:
    current_saved = history.get(data_date, {}) if data_date else {}
    old_saved = history.get(selected_date, {})
    if old_saved:
        def hist_diff(key):
            a, b = current_saved.get(key), old_saved.get(key)
            return a - b if a is not None and b is not None else None

        delta_bottom = hist_diff("bottom_btc")
        delta_top = hist_diff("top_btc")
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

# So sánh trực tiếp ngay trên biểu đồ hiện tại, không cần chuyển radio lịch sử.
comparison_options = {
    "Không so sánh": 0,
    "So với 1 ngày trước": 1,
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

if comparison_days and data_date:
    comparison_date = (
        datetime.strptime(data_date, "%Y-%m-%d").date()
        - timedelta(days=comparison_days)
    ).strftime("%Y-%m-%d")
    comparison_saved = history.get(comparison_date)
    if comparison_saved and comparison_saved.get("urpd"):
        comparison_urpd = records_to_urpd(comparison_saved["urpd"])
        comparison_price = comparison_saved.get("price")
        st.caption(
            f"Đang chồng dữ liệu {data_date} với snapshot {comparison_date}. "
            "Cột hiện tại giữ màu xanh/đỏ; cột lịch sử màu xám để nhìn chênh lệch ngay từng bucket."
        )
    else:
        st.warning(f"Chưa có snapshot URPD cho ngày {comparison_date} để so sánh.")

if chart_urpd is None:
    st.warning("Chưa có dữ liệu URPD để vẽ biểu đồ.")
else:
    urpd = chart_urpd.copy()
    urpd["mid_price"] = (urpd.price_low + urpd.price_high) / 2
    price_for_chart = chart_price if chart_price is not None else price
    colors = np.where(urpd.mid_price < price_for_chart, "#10b981", "#ef4444")

    fig = go.Figure()

    # Vẽ snapshot lịch sử trước để snapshot hiện tại nằm nổi lên trên.
    if comparison_urpd is not None:
        hist = comparison_urpd.copy()
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
            for r in comparison_urpd.itertuples(index=False)
        }
        compare_btc = np.array([
            hist_lookup.get(
                (round(float(lo), 6), round(float(hi), 6)),
                np.nan,
            )
            for lo, hi in zip(urpd.price_low, urpd.price_high)
        ], dtype=float)
        delta_btc = urpd.btc_amount.to_numpy(float) - compare_btc
        current_values = urpd.btc_amount.to_numpy(float)
        delta_pct = np.where(
            np.isfinite(compare_btc) & (compare_btc != 0),
            delta_btc / compare_btc * 100.0,
            np.nan,
        )
        current_customdata = np.column_stack([
            urpd.price_low.to_numpy(float),
            urpd.price_high.to_numpy(float),
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
            [urpd.price_low.to_numpy(float), urpd.price_high.to_numpy(float)], axis=-1
        )
        current_hover = (
            f"Snapshot {chart_date}<br>"
            "Khoảng giá: $%{customdata[0]:,.0f} - $%{customdata[1]:,.0f}<br>"
            "BTC: %{y:,.2f}<extra></extra>"
        )

    fig.add_trace(
        go.Bar(
            x=urpd.mid_price,
            y=urpd.btc_amount,
            width=(urpd.price_high - urpd.price_low) * (0.72 if comparison_urpd is not None else 0.92),
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
        delta_y = urpd.btc_amount.to_numpy(float) + np.maximum(urpd.btc_amount.to_numpy(float) * 0.012, 150.0)
        fig.add_trace(
            go.Scatter(
                x=urpd.mid_price,
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
