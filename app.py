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
st.caption("URPD thực tế từ BGeometrics • Supply in Loss trực tiếp • Không mô phỏng")

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
        "Ngưỡng vùng đỉnh (USD)", value=TOP_START, step=100.0
    )
    bottom_start = st.number_input(
        "Vùng đáy từ (USD)", value=BOTTOM_START, step=100.0
    )
    bottom_end = st.number_input(
        "Vùng đáy đến (USD)", value=BOTTOM_END, step=100.0
    )
    ath = st.number_input("ATH (USD)", value=DEFAULT_ATH, step=100.0)

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

# Nếu không có file, tự lấy ngày hôm qua từ BGeometrics.
if urpd is None:
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        urpd, urpd_url = bgeometrics_urpd(yesterday)
        urpd_source = "BGeometrics /v1/urpd"
        urpd_date = yesterday
    except Exception as e:
        st.warning(f"Chưa lấy được URPD BGeometrics: {e}")

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
    top_btc = btc_in_range(urpd, top_start, float("inf"))
    bottom_btc = btc_in_range(urpd, bottom_start, bottom_end)
else:
    total_urpd = top_btc = bottom_btc = None

ath_discount = (price - ath) / ath * 100 if ath else 0

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Giá BTC hiện tại", f"${price:,.0f}")
c2.metric("Chiết khấu từ ATH", f"{ath_discount:.2f}%")
c3.metric(
    "BTC vùng đỉnh",
    f"{top_btc:,.0f} BTC" if top_btc is not None else "N/A",
    f"{top_btc / total_urpd * 100:.2f}% URPD"
    if total_urpd and top_btc is not None
    else "Chưa có URPD",
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
st.subheader("Bảng cung vùng đỉnh và cung đang lỗ")

summary = pd.DataFrame(
    {
        "Chỉ số": [
            "BTC vùng đỉnh",
            "Tổng BTC theo URPD",
            "% cung vùng đỉnh",
            "Cung đang lỗ trực tiếp",
            "% cung đang lỗ trực tiếp",
            "Ngày dữ liệu URPD",
            "Giá BTC hiện tại",
            "Ngưỡng vùng đỉnh",
            "Nguồn URPD",
            "Nguồn Supply in Loss",
        ],
        "Giá trị": [
            f"{top_btc:,.2f}" if top_btc is not None else "N/A",
            f"{total_urpd:,.2f}" if total_urpd is not None else "N/A",
            f"{top_btc / total_urpd * 100:.4f}%"
            if total_urpd and top_btc is not None
            else "N/A",
            f"{loss_btc:,.2f}" if loss_btc is not None else "N/A",
            f"{loss_percent:.4f}%" if loss_percent is not None else "N/A",
            urpd_date or "N/A",
            f"${price:,.2f}",
            f">= ${top_start:,.0f}",
            urpd_source,
            loss_source or "Chưa có dữ liệu",
        ],
        "Đơn vị": ["BTC", "BTC", "%", "BTC", "%", "", "USD", "USD", "", ""],
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
    "BTC vùng đỉnh được tính trực tiếp từ URPD BGeometrics của ngày hôm qua "
    "(hoặc file URPD tải lên). Supply in Loss vẫn lấy trực tiếp từ ResearchBitcoin."
)

# Lịch sử URPD: lưu toàn bộ bucket gốc theo từng ngày.
# Local: lưu cạnh app.py. Cloud: có thể đồng bộ lên GitHub nếu khai báo secrets.
history_file = "urpd_history.json"


def load_history_local():
    try:
        with open(history_file, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def github_config():
    token = st.secrets.get("GITHUB_TOKEN", os.getenv("GITHUB_TOKEN", ""))
    repo = st.secrets.get("GITHUB_REPO", os.getenv("GITHUB_REPO", ""))
    branch = st.secrets.get("GITHUB_BRANCH", os.getenv("GITHUB_BRANCH", "main"))
    return str(token).strip(), str(repo).strip(), str(branch).strip() or "main"


def github_get_history():
    token, repo, branch = github_config()
    if not token or not repo or "/" not in repo:
        return None
    url = f"https://api.github.com/repos/{repo}/contents/{history_file}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    try:
        r = requests.get(url, headers=headers, params={"ref": branch}, timeout=20)
        if r.status_code == 404:
            return {}, None
        r.raise_for_status()
        payload = r.json()
        content = base64.b64decode(payload["content"]).decode("utf-8")
        value = json.loads(content)
        return (value if isinstance(value, dict) else {}), payload.get("sha")
    except Exception as e:
        st.warning(f"Không đọc được lịch sử GitHub: {e}")
        return None


def github_save_history(history, sha=None):
    token, repo, branch = github_config()
    if not token or not repo or "/" not in repo:
        return False
    url = f"https://api.github.com/repos/{repo}/contents/{history_file}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    content = json.dumps(history, ensure_ascii=False, indent=2).encode("utf-8")
    payload = {
        "message": "Update URPD history",
        "content": base64.b64encode(content).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha
    try:
        r = requests.put(url, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        return True
    except Exception as e:
        st.warning(f"Không đồng bộ được urpd_history.json lên GitHub: {e}")
        return False


def urpd_to_records(df):
    return df[["price_low", "price_high", "btc_amount"]].to_dict("records")


def records_to_urpd(records):
    return normalize_urpd(pd.DataFrame(records))

# Ngày phải lấy từ dữ liệu BGeometrics thực tế, không lấy ngày chạy app.
data_date = urpd_date if isinstance(urpd_date, str) and len(urpd_date) == 10 and urpd_date[4] == "-" else None
history = load_history_local()
github_sha = None
remote = github_get_history()
if remote is not None:
    remote_history, github_sha = remote
    # Gộp lịch sử local và GitHub, không để bản rỗng trên GitHub ghi đè dữ liệu local.
    if isinstance(remote_history, dict):
        merged_history = dict(history)
        merged_history.update(remote_history)
        history = merged_history

if urpd is not None and data_date:
    new_snapshot = {
        "date": data_date,
        "price": float(price) if price is not None else None,
        "top_btc": float(top_btc) if top_btc is not None else None,
        "bottom_btc": float(bottom_btc) if bottom_btc is not None else None,
        "total_urpd": float(total_urpd) if total_urpd is not None else None,
        "urpd": urpd_to_records(urpd),
        "source": urpd_source,
    }
    # Chỉ ghi/đồng bộ khi snapshot ngày đó chưa tồn tại hoặc thực sự thay đổi.
    changed = history.get(data_date) != new_snapshot
    if changed:
        history[data_date] = new_snapshot
        history = dict(sorted(history.items())[-365:])
        try:
            with open(history_file, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            st.warning(f"Không lưu được lịch sử URPD local: {e}")
        if github_config()[0] and github_config()[1]:
            synced = github_save_history(history, github_sha)
            if synced:
                st.success(f"Đã lưu lịch sử ngày {data_date} lên GitHub.")
            else:
                st.error("Không lưu được lịch sử lên GitHub. Kiểm tra quyền ghi của GITHUB_TOKEN.")
        else:
            st.info("Lịch sử chỉ đang lưu tạm trên phiên chạy này vì chưa có GITHUB_TOKEN/GITHUB_REPO.")

# Mốc lịch sử tính từ ngày dữ liệu URPD mới nhất, tránh nhầm khi BGeometrics chậm cập nhật.
latest_data_date = data_date or (max(history) if history else today_key)
if history:
    st.caption(f"Đã nạp {len(history)} ngày lịch sử URPD. Ngày mới nhất: {max(history)}")
else:
    st.warning("Chưa có snapshot lịch sử nào. Hãy bấm Cập nhật dữ liệu từ BGeometrics.")

st.markdown("---")
st.subheader("Lịch sử biến động nguồn cung")
st.caption("Chọn mốc lịch sử để xem lại đúng biểu đồ URPD của ngày đó.")

choices = {
    "Hiện tại": 0,
    "1 ngày trước": 1,
    "7 ngày trước": 7,
    "30 ngày trước": 30,
    "120 ngày trước": 120,
    "180 ngày trước": 180,
}
selected = st.radio("Xem biểu đồ:", list(choices), horizontal=True)

selected_days = choices[selected]
base_date = datetime.strptime(latest_data_date, "%Y-%m-%d").date()
selected_date = (base_date - timedelta(days=selected_days)).strftime("%Y-%m-%d")

if selected_days == 0:
    chart_urpd = urpd
    chart_date = latest_data_date
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
if selected_days > 0 and chart_urpd is not None:
    current_saved = history.get(latest_data_date, {})
    old_saved = history.get(selected_date, {})
    if old_saved:
        def hist_diff(key):
            a, b = current_saved.get(key), old_saved.get(key)
            return a - b if a is not None and b is not None else None

        delta_bottom = hist_diff("bottom_btc")
        delta_top = hist_diff("top_btc")
        delta_total = hist_diff("total_urpd")
        st.write(
            f"Trong kỳ **{selected_date} → {latest_data_date}**, "
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

if chart_urpd is None:
    st.warning("Chưa có dữ liệu URPD để vẽ biểu đồ.")
else:
    urpd = chart_urpd.copy()
    urpd["mid_price"] = (urpd.price_low + urpd.price_high) / 2
    price_for_chart = chart_price if chart_price is not None else price
    colors = np.where(urpd.mid_price < price_for_chart, "#10b981", "#ef4444")
    fig = go.Figure(
        go.Bar(
            x=urpd.mid_price,
            y=urpd.btc_amount,
            width=(urpd.price_high - urpd.price_low) * 0.92,
            marker_color=colors,
            customdata=np.stack(
                [urpd.price_low, urpd.price_high], axis=-1
            ),
            hovertemplate=(
                "Khoảng giá: $%{customdata[0]:,.0f} - "
                "$%{customdata[1]:,.0f}<br>"
                "BTC: %{y:,.2f}<extra></extra>"
            ),
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
