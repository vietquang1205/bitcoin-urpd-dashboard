import os
from datetime import datetime, timedelta, timezone

import requests
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go

st.set_page_config(layout="wide", page_title="BTC URPD Monitor", page_icon="🪙")

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
    token = st.secrets.get("cb1937c4-cd4d-42eb-acc2-3611bf08ff95", "")
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

st.info(
    "BTC vùng đỉnh được tính trực tiếp từ URPD BGeometrics của ngày hôm qua "
    "(hoặc file URPD tải lên). Supply in Loss vẫn lấy trực tiếp từ ResearchBitcoin."
)

st.subheader("Phân bố nguồn cung theo bucket URPD")

if urpd is None:
    st.warning("Chưa có dữ liệu URPD để vẽ biểu đồ.")
else:
    colors = np.where(urpd.mid_price < price, "#10b981", "#ef4444")
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
    fig.add_vline(
        x=price,
        line_dash="dash",
        line_color="white",
        annotation_text=f"Giá hiện tại: ${price:,.0f}",
    )
    fig.add_vline(
        x=top_start,
        line_dash="dot",
        line_color="#f59e0b",
        annotation_text=f"Vùng đỉnh từ ${top_start:,.0f}",
    )
    fig.update_layout(
        template="plotly_dark",
        height=500,
        xaxis_title="Giá vốn on-chain ($)",
        yaxis_title="BTC",
        margin=dict(l=40, r=40, t=35, b=40),
        bargap=0.02,
    )
    st.plotly_chart(fig, use_container_width=True)

    with st.expander("Xem URPD gốc"):
        st.dataframe(urpd, hide_index=True, use_container_width=True)
