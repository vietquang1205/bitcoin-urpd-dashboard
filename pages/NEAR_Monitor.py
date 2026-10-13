import requests
from pathlib import Path
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go

st.set_page_config(page_title="NEAR Monitor", page_icon="🟢", layout="wide")

st.title("🟢 NEAR Monitor")
st.caption("NEAR chạy độc lập với BTC. API chỉ được gọi sau khi ông bấm nút tải dữ liệu.")

# Không có request HTTP ở top-level. Chỉ định nghĩa hàm ở đây.

# NEAR MONITOR — free/public data layer
# =========================
NEAR_CG_ID = "near"
NEAR_RPC_URL = "https://rpc.mainnet.fastnear.com"
try:
    NEARBLOCKS_API_KEY = st.secrets.get("NEARBLOCKS_API_KEY", "")
except Exception:
    NEARBLOCKS_API_KEY = ""


def _near_num(x, default=np.nan):
    try:
        v = float(x)
        return v if np.isfinite(v) else default
    except Exception:
        return default


def _near_fmt_usd(v):
    if v is None or not np.isfinite(_near_num(v)):
        return "N/A"
    v = float(v)
    if abs(v) >= 1e12:
        return f"${v/1e12:.2f}T"
    if abs(v) >= 1e9:
        return f"${v/1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"${v/1e6:.2f}M"
    if abs(v) >= 1e3:
        return f"${v/1e3:.1f}K"
    return f"${v:,.0f}"


def _near_fmt_near(v):
    if v is None or not np.isfinite(_near_num(v)):
        return "N/A"
    return f"{float(v):,.0f} Ⓝ"


@st.cache_data(ttl=300, show_spinner=False)
def _coingecko_market_data():
    url = f"https://api.coingecko.com/api/v3/coins/{NEAR_CG_ID}"
    params = {
        "localization": "false", "tickers": "false", "market_data": "true",
        "community_data": "false", "developer_data": "false", "sparkline": "false",
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    j = r.json()
    md = j.get("market_data", {})
    usd = md.get("current_price", {})
    return {
        "price": _near_num(usd.get("usd")),
        "market_cap": _near_num(md.get("market_cap", {}).get("usd")),
        "fdv": _near_num(md.get("fully_diluted_valuation", {}).get("usd")),
        "volume_24h": _near_num(md.get("total_volume", {}).get("usd")),
        "change_24h": _near_num(md.get("price_change_percentage_24h")),
        "circulating_supply": _near_num(md.get("circulating_supply")),
        "total_supply": _near_num(md.get("total_supply")),
        "max_supply": _near_num(md.get("max_supply")),
        "updated": j.get("last_updated"),
    }


@st.cache_data(ttl=120, show_spinner=False)
def _binance_near_ticker():
    r = requests.get(
        "https://api.binance.com/api/v3/ticker/24hr",
        params={"symbol": "NEARUSDT"}, timeout=15
    )
    r.raise_for_status()
    j = r.json()
    return {
        "price": _near_num(j.get("lastPrice")),
        "volume_24h": _near_num(j.get("quoteVolume")),
        "change_24h": _near_num(j.get("priceChangePercent")),
    }


@st.cache_data(ttl=900, show_spinner=False)
def _binance_near_chart(days=90):
    r = requests.get(
        "https://api.binance.com/api/v3/klines",
        params={"symbol": "NEARUSDT", "interval": "1d", "limit": min(int(days), 1000)},
        timeout=20,
    )
    r.raise_for_status()
    rows = []
    for row in r.json():
        try:
            rows.append({"date": pd.to_datetime(row[0], unit="ms", utc=True), "price": float(row[4])})
        except Exception:
            pass
    return pd.DataFrame(rows).drop_duplicates("date").sort_values("date")


@st.cache_data(ttl=900, show_spinner=False)
def _near_heatmap_candles(days=30):
    """Lấy nến NEARUSDT công khai từ Binance; không cần API key."""
    days = int(days)
    if days <= 30:
        interval = "1h"
        limit = min(1000, days * 24)
    else:
        interval = "4h"
        limit = min(1000, days * 6)

    endpoints = [
        "https://data-api.binance.vision/api/v3/klines",
        "https://api.binance.com/api/v3/klines",
    ]
    params = {"symbol": "NEARUSDT", "interval": interval, "limit": limit}
    last_error = None
    for url in endpoints:
        try:
            response = requests.get(url, params=params, timeout=20)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list) or not payload:
                raise ValueError("Binance không trả dữ liệu nến NEARUSDT.")
            rows = []
            for row in payload:
                try:
                    rows.append({
                        "time": pd.to_datetime(row[0], unit="ms", utc=True),
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "base_volume": float(row[5]),
                        "quote_volume": float(row[7]),
                        "trades": int(row[8]),
                    })
                except (TypeError, ValueError, IndexError):
                    continue
            df = pd.DataFrame(rows)
            if df.empty:
                raise ValueError("Không có nến hợp lệ.")
            return df.sort_values("time").drop_duplicates("time").reset_index(drop=True)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Không lấy được nến NEARUSDT từ Binance: {last_error}")


def render_near_heatmap():
    """Glassnode-style NEAR heatmap with a volume profile and estimated S/R zones."""
    from plotly.subplots import make_subplots

    st.markdown("---")
    st.subheader("🌡️ NEAR Heatmap — Volume Profile & vùng hỗ trợ/kháng cự")
    st.caption(
        "Bản đồ nhiệt theo thời gian × vùng giá; thanh bên phải cộng dồn khối lượng USDT ước tính theo giá. "
        "Hỗ trợ/kháng cự được gợi ý từ các đỉnh volume profile, không phải mức giá chắc chắn đảo chiều."
    )

    c1, c2, c3 = st.columns([1, 1, 1])
    with c1:
        days = st.selectbox(
            "Khoảng thời gian", [7, 30, 60, 90], index=2,
            format_func=lambda d: f"{d} ngày", key="near_heatmap_days",
        )
    with c2:
        scope = st.radio(
            "Phạm vi giá", ["Quanh giá hiện tại", "Toàn bộ vùng giá"],
            index=0, key="near_heatmap_scope",
        )
    with c3:
        bins = st.select_slider(
            "Độ chi tiết vùng giá", options=[40, 60, 80, 100],
            value=60, key="near_heatmap_bins",
        )

    try:
        with st.spinner("Đang tải dữ liệu giao dịch NEAR..."):
            candles = _near_heatmap_candles(int(days))
    except Exception as exc:
        st.warning(f"NEAR Heatmap chưa tải được dữ liệu: {exc}")
        return

    candles = candles.sort_values("time").reset_index(drop=True)
    latest_price = float(candles["close"].iloc[-1])
    if scope == "Quanh giá hiện tại":
        price_min = max(float(candles["low"].min()), latest_price * 0.70)
        price_max = min(float(candles["high"].max()), latest_price * 1.30)
    else:
        price_min = float(candles["low"].min())
        price_max = float(candles["high"].max())
    if not np.isfinite(price_min) or not np.isfinite(price_max) or price_max <= price_min:
        price_min, price_max = max(0.0, latest_price * 0.7), latest_price * 1.3

    edges = np.linspace(price_min, price_max, int(bins) + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    z = np.zeros((int(bins), len(candles)), dtype=float)
    lows = candles["low"].to_numpy(dtype=float)
    highs = candles["high"].to_numpy(dtype=float)
    volumes = candles["quote_volume"].to_numpy(dtype=float)

    # Allocate quote volume proportionally over the visible price span of each candle.
    for j, (lo, hi, volume) in enumerate(zip(lows, highs, volumes)):
        if volume <= 0 or hi < price_min or lo > price_max:
            continue
        clipped_lo, clipped_hi = max(lo, price_min), min(hi, price_max)
        if hi <= lo:
            idx = int(np.clip(np.searchsorted(edges, lo, side="right") - 1, 0, len(centers) - 1))
            z[idx, j] += volume
            continue
        first = max(0, int(np.searchsorted(edges, clipped_lo, side="right") - 1))
        last = min(len(centers) - 1, int(np.searchsorted(edges, clipped_hi, side="left")))
        idxs = np.arange(first, last + 1)
        overlap = np.maximum(0.0, np.minimum(edges[idxs + 1], clipped_hi) - np.maximum(edges[idxs], clipped_lo))
        denom = float(overlap.sum())
        if denom > 0:
            z[idxs, j] += volume * overlap / denom

    # Volume profile by price, smoothing only for peak detection (not for displayed bars).
    profile = np.nansum(z, axis=1)
    smooth = np.convolve(profile, np.ones(5) / 5, mode="same") if len(profile) >= 5 else profile.copy()
    peak_idxs = []
    for i in range(1, len(smooth) - 1):
        if smooth[i] >= smooth[i - 1] and smooth[i] >= smooth[i + 1] and smooth[i] > 0:
            peak_idxs.append(i)
    peak_idxs = sorted(peak_idxs, key=lambda i: smooth[i], reverse=True)
    selected_peaks = []
    min_gap = max(2, int(bins) // 18)
    for idx in peak_idxs:
        if all(abs(idx - other) >= min_gap for other in selected_peaks):
            selected_peaks.append(idx)
        if len(selected_peaks) >= 8:
            break

    supports = sorted([i for i in selected_peaks if centers[i] < latest_price], key=lambda i: latest_price - centers[i])[:2]
    resistances = sorted([i for i in selected_peaks if centers[i] > latest_price], key=lambda i: centers[i] - latest_price)[:2]

    fig = make_subplots(
        rows=1, cols=2, shared_yaxes=True, horizontal_spacing=0.018,
        column_widths=[0.83, 0.17],
        subplot_titles=("Khối lượng ước tính theo thời gian và giá", "Volume Profile"),
    )
    z_plot = np.where(z > 0, z, np.nan)
    positive = z[z > 0]
    zmax = float(np.percentile(positive, 98)) if len(positive) else 1.0
    zmax = max(zmax, 1.0)
    fig.add_trace(go.Heatmap(
        x=candles["time"], y=centers, z=z_plot,
        colorscale=[
            [0.00, "#160d2b"], [0.18, "#35205f"], [0.36, "#433e9a"],
            [0.54, "#2879b9"], [0.70, "#18b4a1"], [0.85, "#73d65b"], [1.00, "#f5f66e"],
        ], zmin=0, zmax=zmax,
        colorbar={"title": "Volume<br>USDT", "thickness": 13, "len": 0.82, "y": 0.52},
        customdata=np.repeat(profile[:, None], len(candles), axis=1),
        hovertemplate=(
            "Thời gian: %{x|%d/%m/%Y %H:%M UTC}<br>Vùng giá: $%{y:.4f}<br>"
            "Volume nến phân bổ: %{z:,.0f} USDT<extra></extra>"
        ), name="Volume heatmap", zsmooth=False,
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=candles["time"], y=candles["close"], mode="lines",
        line={"color": "#ffffff", "width": 1.7}, name="Giá NEAR",
        hovertemplate="%{x|%d/%m/%Y %H:%M UTC}<br>NEAR: $%{y:.4f}<extra></extra>",
    ), row=1, col=1)
    fig.add_trace(go.Bar(
        x=profile, y=centers, orientation="h", name="Volume Profile",
        marker={"color": ["#ff657a" if y > latest_price else "#39c995" for y in centers], "opacity": 0.88},
        customdata=profile,
        hovertemplate="Vùng giá: $%{y:.4f}<br>Volume cộng dồn: $%{x:,.0f}<extra></extra>",
    ), row=1, col=2)

    # Support/resistance bands based on local volume-profile peaks.
    band_half = (edges[1] - edges[0]) * 1.35
    for idx in supports:
        y0, y1 = max(price_min, centers[idx] - band_half), min(price_max, centers[idx] + band_half)
        fig.add_hrect(y0=y0, y1=y1, fillcolor="#23c98b", opacity=0.17, line_width=0, row=1, col=1)
        fig.add_hline(y=centers[idx], line_color="#39d6a0", line_dash="dot", line_width=1.2, row=1, col=1,
                      annotation_text=f"Hỗ trợ ~ ${centers[idx]:.4f}", annotation_position="bottom left")
    for idx in resistances:
        y0, y1 = max(price_min, centers[idx] - band_half), min(price_max, centers[idx] + band_half)
        fig.add_hrect(y0=y0, y1=y1, fillcolor="#ff526d", opacity=0.16, line_width=0, row=1, col=1)
        fig.add_hline(y=centers[idx], line_color="#ff6c80", line_dash="dot", line_width=1.2, row=1, col=1,
                      annotation_text=f"Cản ~ ${centers[idx]:.4f}", annotation_position="top left")

    fig.add_hline(y=latest_price, line_dash="dash", line_color="#ffffff", line_width=1.4, row=1, col=1,
                  annotation_text=f"NEAR ${latest_price:.4f}", annotation_position="top right")
    fig.update_layout(
        template="plotly_dark", height=650,
        margin=dict(l=15, r=15, t=55, b=25), hovermode="closest",
        paper_bgcolor="#0e1117", plot_bgcolor="#0e1117",
        legend=dict(orientation="h", yanchor="bottom", y=1.03, x=0),
        bargap=0.08,
    )
    fig.update_yaxes(range=[price_min, price_max], tickprefix="$", gridcolor="rgba(150,150,150,.12)", row=1, col=1)
    fig.update_yaxes(range=[price_min, price_max], showticklabels=False, row=1, col=2)
    fig.update_xaxes(title_text="Thời gian (UTC)", gridcolor="rgba(150,150,150,.10)", row=1, col=1)
    fig.update_xaxes(title_text="USDT", tickformat="~s", gridcolor="rgba(150,150,150,.10)", row=1, col=2)
    st.plotly_chart(fig, use_container_width=True, config={"displaylogo": False, "scrollZoom": True})

    # Clear support/resistance summary cards.
    a, b, c = st.columns(3)
    a.metric("Giá NEAR mới nhất", f"${latest_price:.4f}")
    a.caption("Đường trắng nét đứt")
    if supports:
        near_support = centers[supports[0]]
        b.metric("Hỗ trợ gần nhất (ước tính)", f"${near_support:.4f}", f"{(near_support/latest_price-1)*100:+.2f}% so với giá")
    else:
        b.metric("Hỗ trợ gần nhất", "Chưa xác định")
    if resistances:
        near_resistance = centers[resistances[0]]
        c.metric("Kháng cự gần nhất (ước tính)", f"${near_resistance:.4f}", f"{(near_resistance/latest_price-1)*100:+.2f}% so với giá")
    else:
        c.metric("Kháng cự gần nhất", "Chưa xác định")

    d, e = st.columns(2)
    d.metric("Khối lượng USDT trong khoảng", f"${candles['quote_volume'].sum():,.0f}")
    e.metric("Số nến", f"{len(candles):,}")
    st.caption(
        "Cách đọc: vùng xanh là hỗ trợ tiềm năng bên dưới giá; vùng đỏ là kháng cự tiềm năng bên trên giá; "
        "thanh bên phải là volume profile cộng dồn. Đây là vùng tham khảo được suy ra từ khối lượng nến OHLCV, "
        "không phải dữ liệu khớp lệnh chính xác theo từng mức giá và không đảm bảo giá sẽ bật/đảo chiều."
    )


def near_market_data():
    """CoinGecko first, Binance fallback. A 429 must not kill the dashboard."""
    try:
        data = _coingecko_market_data()
        data["_source"] = "CoinGecko"
        return data
    except Exception as cg_error:
        try:
            b = _binance_near_ticker()
            return {
                "price": b["price"], "market_cap": np.nan, "fdv": np.nan,
                "volume_24h": b["volume_24h"], "change_24h": b["change_24h"],
                "circulating_supply": np.nan, "total_supply": np.nan, "max_supply": np.nan,
                "updated": None, "_source": "Binance fallback",
                "_fallback_error": str(cg_error),
            }
        except Exception as bin_error:
            return {
                "price": np.nan, "market_cap": np.nan, "fdv": np.nan,
                "volume_24h": np.nan, "change_24h": np.nan,
                "circulating_supply": np.nan, "total_supply": np.nan, "max_supply": np.nan,
                "updated": None, "_source": "Unavailable",
                "_fallback_error": f"CoinGecko: {cg_error}; Binance: {bin_error}",
            }


def near_market_chart(days=90):
    try:
        url = f"https://api.coingecko.com/api/v3/coins/{NEAR_CG_ID}/market_chart"
        r = requests.get(url, params={"vs_currency": "usd", "days": days}, timeout=25)
        r.raise_for_status()
        rows = []
        for ts, price in r.json().get("prices", []):
            try:
                rows.append({"date": pd.to_datetime(ts, unit="ms", utc=True), "price": float(price)})
            except Exception:
                pass
        df = pd.DataFrame(rows).drop_duplicates("date").sort_values("date")
        if not df.empty:
            return df
    except Exception:
        pass
    try:
        return _binance_near_chart(days)
    except Exception:
        return pd.DataFrame(columns=["date", "price"])


def _near_rpc(method, params=None):
    payload = {
        "jsonrpc": "2.0",
        "id": "near-monitor",
        "method": method,
        "params": params if params is not None else [],
    }
    r = requests.post(
        NEAR_RPC_URL,
        json=payload,
        headers={"content-type": "application/json"},
        timeout=20,
    )
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(j["error"])
    return j.get("result", {})


@st.cache_data(ttl=600, show_spinner=False)
def near_network_data():
    status = _near_rpc("status", [])
    validators = _near_rpc("validators", [None])
    current = validators.get("current_validators", []) or []
    total_stake_yocto = 0.0
    for v in current:
        total_stake_yocto += _near_num(v.get("stake"), 0.0)
    return {
        "chain_id": status.get("chain_id", "NEAR"),
        "latest_block": (status.get("sync_info") or {}).get("latest_block_height"),
        "validator_count": len(current),
        "total_stake_near": total_stake_yocto / 1e24,
        "validators": current,
    }


@st.cache_data(ttl=900, show_spinner=False)
def near_defillama_data():
    # DeFiLlama's current chain data is served from /v2/chains.
    # The old /v2/chain/Near URL returns 404, so do not call it.
    chains_url = "https://api.llama.fi/v2/chains"
    r1 = requests.get(chains_url, timeout=25)
    r1.raise_for_status()
    chains = r1.json()
    if not isinstance(chains, list):
        raise ValueError("DeFiLlama /v2/chains không trả về danh sách chain.")

    chain = next(
        (row for row in chains if isinstance(row, dict) and str(row.get("name", "")).strip().lower() == "near"),
        None,
    )
    if chain is None:
        raise ValueError("Không tìm thấy chain Near trong DeFiLlama /v2/chains.")

    # /charts/{chain} is the public historical chain TVL endpoint.
    # If history is temporarily unavailable, keep current metrics usable.
    hist_df = pd.DataFrame(columns=["date", "tvl"])
    try:
        hist_url = "https://api.llama.fi/charts/Near"
        r2 = requests.get(hist_url, timeout=25)
        r2.raise_for_status()
        hist = r2.json()
        hist_rows = []
        for row in hist if isinstance(hist, list) else []:
            if isinstance(row, dict):
                ts = row.get("date")
                tvl = row.get("tvl")
                try:
                    if isinstance(ts, (int, float)):
                        dt = pd.to_datetime(ts, unit="s", utc=True)
                    else:
                        dt = pd.to_datetime(ts, utc=True)
                    hist_rows.append({"date": dt, "tvl": float(tvl)})
                except Exception:
                    pass
        if hist_rows:
            hist_df = pd.DataFrame(hist_rows).sort_values("date").reset_index(drop=True)
    except Exception:
        # Do not fail the entire NEAR dashboard just because historical TVL is unavailable.
        pass

    return {"chain": chain, "history": hist_df}


def _near_hist_change(hist_df, days):
    if hist_df is None or hist_df.empty:
        return np.nan
    end = hist_df.iloc[-1]["tvl"]
    cutoff = hist_df.iloc[-1]["date"] - pd.Timedelta(days=days)
    old = hist_df[hist_df["date"] <= cutoff]
    if old.empty:
        return np.nan
    base = float(old.iloc[-1]["tvl"])
    return (float(end) / base - 1.0) * 100.0 if base > 0 else np.nan


def _near_ema(series, span):
    return float(pd.Series(series).ewm(span=span, adjust=False).mean().iloc[-1]) if len(series) else np.nan


def _near_score_components(market, chart, defi, network):
    p = _near_num(market.get("price"))
    c24 = _near_num(market.get("change_24h"), 0.0)
    p7 = np.nan
    p30 = np.nan
    vol_ratio = np.nan
    if chart is not None and not chart.empty:
        s = chart.set_index("date")["price"].sort_index()
        now = float(s.iloc[-1])
        for d, name in [(7, "p7"), (30, "p30")]:
            old = s[s.index <= s.index[-1] - pd.Timedelta(days=d)]
            if not old.empty and old.iloc[-1] > 0:
                change = (now / float(old.iloc[-1]) - 1.0) * 100.0
                if name == "p7":
                    p7 = change
                else:
                    p30 = change
        vol = market.get("volume_24h")
        if vol and len(s) > 7:
            vol_ratio = float(vol) / max(float(s.pct_change().rolling(7).std().iloc[-1] or 0), 1e-9)

    hist = defi.get("history") if isinstance(defi, dict) else None
    tvl7 = _near_hist_change(hist, 7)
    tvl30 = _near_hist_change(hist, 30)

    # Transparent composite: no proprietary/AI claim.
    momentum = float(np.clip(50 + (0 if np.isnan(p7) else p7 * 2.0) + c24 * 1.0, 0, 100))
    ecosystem = float(np.clip(50 + (0 if np.isnan(tvl7) else tvl7 * 1.8) + (0 if np.isnan(tvl30) else tvl30 * 0.6), 0, 100))
    network = float(np.clip(50 + min(network.get("validator_count", 0), 500) * 0.04, 50, 75))
    risk = float(np.clip(50 + (0 if np.isnan(p30) else abs(p30) * 0.8) + (0 if np.isnan(c24) else max(c24, 0) * 0.4), 0, 100))
    flow = float(np.clip(50 + (0 if np.isnan(tvl7) else tvl7 * 2.2), 0, 100))
    total = float(np.clip(momentum * .30 + flow * .25 + network * .20 + ecosystem * .25, 0, 100))
    return {
        "momentum": momentum,
        "flow": flow,
        "network": network,
        "ecosystem": ecosystem,
        "risk": risk,
        "total": total,
        "p7": p7,
        "p30": p30,
        "tvl7": tvl7,
        "tvl30": tvl30,
        "ema20": _near_ema(chart["price"].to_numpy(), 20) if chart is not None and not chart.empty else np.nan,
        "ema50": _near_ema(chart["price"].to_numpy(), 50) if chart is not None and not chart.empty else np.nan,
        "ema200": _near_ema(chart["price"].to_numpy(), 200) if chart is not None and not chart.empty else np.nan,
    }


def _near_holder_flow():
    """Optional NearBlocks holder view. Chỉ bật nếu người dùng đã thêm API key."""
    if not NEARBLOCKS_API_KEY:
        return None
    url = "https://api.nearblocks.io/v1/stats"
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {NEARBLOCKS_API_KEY}"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()



# ============================================================
# NEAR WHALE MONITOR V2
# ============================================================
WHALE_HISTORY_FILE = "near_whale_history.json"
DEFAULT_WHALE_CONFIG = """# account.near | label | type
# example.near | Whale #1 | whale
# binance.near | Binance | exchange
"""


def _whale_rpc_account(account_id):
    payload = {
        "jsonrpc": "2.0", "id": "near-whale-monitor", "method": "query",
        "params": {"request_type": "view_account", "finality": "final", "account_id": account_id.strip()},
    }
    r = requests.post(NEAR_RPC_URL, json=payload, headers={"content-type": "application/json"}, timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(j["error"])
    result = j.get("result") or {}
    amount = _near_num(result.get("amount"), 0.0) / 1e24
    locked = _near_num(result.get("locked"), 0.0) / 1e24
    return {"balance": amount + locked, "liquid": amount, "locked": locked}


def _load_whale_history():
    import json
    path = Path(WHALE_HISTORY_FILE)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_whale_history(rows):
    import json
    cleaned = [r for r in rows if isinstance(r, dict) and r.get("date")]
    cleaned.sort(key=lambda x: str(x["date"]))
    dedup = {str(r["date"]): r for r in cleaned}
    Path(WHALE_HISTORY_FILE).write_text(
        json.dumps(list(dedup.values()), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _parse_whale_config(text):
    rows = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.split("|")]
        if len(parts) < 3:
            continue
        account, label, kind = parts[:3]
        kind = kind.lower()
        if kind not in {"whale", "exchange", "validator", "protocol", "unknown"}:
            kind = "unknown"
        rows.append({"account": account, "label": label or account, "type": kind})
    return rows


def _whale_snapshot(config_rows):
    from datetime import datetime, timezone
    wallets, errors = [], []
    now = datetime.now(timezone.utc)
    for item in config_rows:
        try:
            bal = _whale_rpc_account(item["account"])
            wallets.append({
                "account": item["account"], "label": item["label"], "type": item["type"],
                "balance": bal["balance"], "liquid": bal["liquid"], "locked": bal["locked"],
            })
        except Exception as e:
            errors.append(f'{item["account"]}: {e}')
    return {
        "date": now.strftime("%Y-%m-%d"), "timestamp": now.isoformat(),
        "wallets": wallets, "errors": errors,
    }


def _whale_daily_series(history, kind="whale"):
    rows = []
    for snap in history:
        total = sum(
            _near_num(w.get("balance"), 0.0)
            for w in (snap.get("wallets") or []) if w.get("type") == kind
        )
        rows.append({"date": snap.get("date"), "balance": total})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
    df["balance"] = pd.to_numeric(df["balance"], errors="coerce").fillna(0)
    return df.dropna(subset=["date"]).drop_duplicates("date").sort_values("date")


def _whale_delta(df, days):
    if df is None or df.empty:
        return np.nan
    end = float(df.iloc[-1]["balance"])
    cutoff = df.iloc[-1]["date"] - pd.Timedelta(days=days)
    old = df[df["date"] <= cutoff]
    if old.empty:
        return np.nan
    return end - float(old.iloc[-1]["balance"])


def _whale_score(netflow, holdings):
    if not np.isfinite(netflow) or holdings <= 0:
        return np.nan
    return float(np.clip(50 + (netflow / holdings) * 1000, 0, 100))


def render_whale_monitor():
    st.markdown("---")
    st.header("🐋 NEAR Whale Monitor V2")
    st.caption(
        "Theo dõi thay đổi số dư native NEAR của các ví ông đánh dấu. "
        "Dương = số dư tăng, âm = số dư giảm. Không đồng nhất balance change với lệnh mua/bán."
    )
    if "whale_config_text" not in st.session_state:
        st.session_state["whale_config_text"] = DEFAULT_WHALE_CONFIG

    with st.expander("⚙️ Cấu hình ví theo dõi", expanded=False):
        st.caption("Mỗi dòng: account.near | tên hiển thị | loại.")
        text_value = st.text_area(
            "Danh sách ví", value=st.session_state["whale_config_text"],
            height=180, key="whale_config_editor"
        )
        if st.button("💾 Lưu danh sách ví", key="save_whale_config"):
            st.session_state["whale_config_text"] = text_value
            st.success("Đã lưu danh sách ví cho phiên hiện tại.")

    config_rows = _parse_whale_config(st.session_state.get("whale_config_text", ""))
    if not config_rows:
        st.info("Chưa có ví theo dõi. Nhập danh sách ví ở phần cấu hình rồi bấm Lưu.")
        return

    history = _load_whale_history()
    c1, c2, c3 = st.columns([1, 1, 2])
    with c1:
        if st.button("🐋 Cập nhật whale", type="primary", key="update_whale"):
            with st.spinner("Đang đọc balance native NEAR..."):
                snap = _whale_snapshot(config_rows)
            if snap["wallets"]:
                history.append(snap)
                _save_whale_history(history)
                history = _load_whale_history()
                st.success(f"Đã lưu snapshot {snap['date']}: {len(snap['wallets'])} ví.")
            if snap["errors"]:
                st.warning("Một số ví không đọc được:\n\n" + "\n".join(snap["errors"]))
    with c2:
        if st.button("🗑️ Xóa history whale", key="clear_whale_cache"):
            _save_whale_history([])
            history = []
            st.success("Đã xóa history whale cục bộ.")
    with c3:
        if history:
            import json
            st.download_button(
                "⬇️ Tải history JSON", data=json.dumps(history, ensure_ascii=False, indent=2),
                file_name="near_whale_history.json", mime="application/json",
                key="download_whale_history"
            )

    if not history:
        st.info("Chưa có snapshot whale. Bấm **Cập nhật whale** để tạo ngày đầu tiên.")
        return

    whale_df = _whale_daily_series(history, "whale")
    exchange_df = _whale_daily_series(history, "exchange")
    latest = history[-1]
    current_wallets = latest.get("wallets") or []
    whale_total = sum(_near_num(w.get("balance"), 0.0) for w in current_wallets if w.get("type") == "whale")
    exchange_total = sum(_near_num(w.get("balance"), 0.0) for w in current_wallets if w.get("type") == "exchange")
    d24, d7, d30 = _whale_delta(whale_df, 1), _whale_delta(whale_df, 7), _whale_delta(whale_df, 30)
    score = _whale_score(d24, whale_total)

    st.subheader("📌 Whale Positioning")
    a,b,c,d,e = st.columns(5)
    a.metric("Whale holdings", _near_fmt_near(whale_total))
    b.metric("Netflow 24h", _near_fmt_near(d24) if np.isfinite(d24) else "N/A")
    c.metric("Netflow 7D", _near_fmt_near(d7) if np.isfinite(d7) else "N/A")
    d.metric("Netflow 30D", _near_fmt_near(d30) if np.isfinite(d30) else "N/A")
    e.metric("Whale Score", f"{score:.0f}/100" if np.isfinite(score) else "N/A")

    if np.isfinite(d24):
        state = "🟢 TÍCH LŨY" if d24 > 0 else ("🔴 PHÂN PHỐI" if d24 < 0 else "🟡 TRUNG TÍNH")
        st.info(f"**24H: {state}** — thay đổi {d24:+,.0f} NEAR. Đây là balance flow.")
    else:
        st.info("Cần snapshot ở ngày trước để tính netflow.")

    ch1,ch2 = st.columns(2)
    with ch1:
        st.subheader("📈 Whale Holdings")
        if not whale_df.empty:
            fig=go.Figure(go.Scatter(x=whale_df["date"], y=whale_df["balance"], mode="lines+markers", name="Whale holdings"))
            fig.update_layout(height=320, margin=dict(l=10,r=10,t=20,b=20), hovermode="x unified", yaxis_title="NEAR")
            st.plotly_chart(fig, use_container_width=True)
    with ch2:
        st.subheader("📊 Whale Netflow")
        if len(whale_df)>=2:
            flow_df=whale_df.copy()
            flow_df["netflow"]=flow_df["balance"].diff()
            fig=go.Figure(go.Bar(x=flow_df["date"], y=flow_df["netflow"], name="Daily netflow"))
            fig.update_layout(height=320, margin=dict(l=10,r=10,t=20,b=20), hovermode="x unified", yaxis_title="NEAR")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Cần thêm snapshot ngày tiếp theo để vẽ netflow.")

    st.subheader("🏦 Exchange Balance Flow")
    if not exchange_df.empty:
        ex24=_whale_delta(exchange_df,1)
        st.metric("Tracked exchange balance", _near_fmt_near(exchange_total), f"{ex24:+,.0f} NEAR" if np.isfinite(ex24) else None)
        st.caption("Exchange balance tăng = NEAR nằm ở nhóm ví sàn được đánh dấu nhiều hơn; không tự động đồng nghĩa đã bán.")
        fig=go.Figure(go.Scatter(x=exchange_df["date"], y=exchange_df["balance"], mode="lines+markers", name="Exchange balance"))
        fig.update_layout(height=280, margin=dict(l=10,r=10,t=20,b=20), yaxis_title="NEAR")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Chưa có ví nào được đánh dấu `exchange`.")

    st.subheader("👛 Ví đang theo dõi")
    latest_rows=[{
        "Ví":w.get("label") or w.get("account"), "Account":w.get("account"), "Loại":w.get("type"),
        "Balance":_near_num(w.get("balance"),0.0), "Liquid":_near_num(w.get("liquid"),0.0),
        "Locked":_near_num(w.get("locked"),0.0)
    } for w in current_wallets]
    if latest_rows:
        st.dataframe(pd.DataFrame(latest_rows).sort_values("Balance",ascending=False), hide_index=True, use_container_width=True)

    st.caption(
        f"History hiện có {len(history)} snapshot. Snapshot mới nhất: {latest.get('date','N/A')}. "
        "Nên tải JSON xuống để backup trước khi redeploy."
    )



@st.cache_data(ttl=900, show_spinner=False)
def _nearblocks_account_balance_history(account_id, api_key):
    """Fetch indexed historical native-NEAR balance data for one account."""
    account_id = str(account_id).strip()
    if not account_id:
        raise ValueError("Account ID đang trống.")
    url = f"https://api.nearblocks.io/v3/accounts/{account_id}/stats/balance"
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {api_key}", "accept": "application/json"},
        timeout=25,
    )
    response.raise_for_status()
    payload = response.json()
    date_keys = ("timestamp", "date", "time", "block_timestamp", "datetime", "created_at")
    balance_keys = ("balance", "near_balance", "amount", "total_balance", "value")
    found = []

    def walk(node):
        if isinstance(node, dict):
            date_value = next((node.get(k) for k in date_keys if node.get(k) is not None), None)
            balance_value = next((node.get(k) for k in balance_keys if node.get(k) is not None), None)
            if date_value is not None and balance_value is not None:
                try:
                    if isinstance(date_value, (int, float)) or str(date_value).strip().isdigit():
                        stamp = float(date_value)
                        if stamp > 1e12:
                            stamp /= 1000.0
                        dt = pd.to_datetime(stamp, unit="s", utc=True, errors="coerce")
                    else:
                        dt = pd.to_datetime(date_value, utc=True, errors="coerce")
                    balance = float(balance_value)
                    if pd.notna(dt) and np.isfinite(balance):
                        if abs(balance) >= 1e18:
                            balance /= 1e24
                        found.append({"date": dt, "balance": balance})
                except (TypeError, ValueError, OverflowError):
                    pass
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    if not found:
        raise ValueError(
            "API phản hồi nhưng không nhận diện được chuỗi ngày/số dư. "
            "Schema có thể đã đổi hoặc endpoint không trả lịch sử cho tài khoản này."
        )
    df = pd.DataFrame(found).drop_duplicates("date").sort_values("date").reset_index(drop=True)
    if df.empty:
        raise ValueError("Không có điểm lịch sử hợp lệ.")
    return df


def _near_holder_suggested_accounts(max_accounts=10):
    """Build a conservative starter list from live validator data and public ecosystem accounts.
    This is NOT a network-wide richest-holder ranking.
    """
    candidates = []
    seen = set()

    # Public ecosystem accounts are candidates, not assumed whales or exchange wallets.
    public_accounts = [
        ("foundation.near", "NEAR Foundation treasury", "protocol"),
        ("wrap.near", "Wrapped NEAR contract", "protocol"),
        ("v2.ref-finance.near", "Ref Finance", "protocol"),
        ("meta-pool.near", "Meta Pool", "protocol"),
    ]
    for account, label, kind in public_accounts:
        try:
            _whale_rpc_account(account)  # verify the account exists before suggesting it
            if account not in seen:
                candidates.append({"account": account, "label": label, "type": kind})
                seen.add(account)
        except Exception:
            # Endpoint may be unavailable or the public account may have changed.
            continue

    # The public RPC provides current validators and their delegated stake.
    try:
        result = _near_rpc("validators", [None])
        validators = result.get("current_validators", []) or []
        validators = sorted(
            validators,
            key=lambda row: _near_num(row.get("stake"), 0.0),
            reverse=True,
        )
        for row in validators:
            account = str(row.get("account_id") or "").strip()
            if not account or account in seen:
                continue
            candidates.append({
                "account": account,
                "label": f"Validator pool — {account}",
                "type": "validator",
            })
            seen.add(account)
            if len(candidates) >= int(max_accounts):
                break
    except Exception as exc:
        if not candidates:
            raise RuntimeError(f"Không lấy được danh sách validator từ NEAR RPC: {exc}") from exc

    return candidates[:int(max_accounts)]


def render_near_holder_history():
    """Historical balances for user-configured accounts; does not infer cost basis."""
    st.markdown("---")
    st.subheader("🐋 Holder Flow & Investor Positioning — lịch sử số dư ví")
    st.caption(
        "Nguồn: NearBlocks API. Theo dõi lịch sử native NEAR của các ví được chọn; "
        "không đại diện toàn bộ holder NEAR và không phải dữ liệu giá vốn."
    )
    if not NEARBLOCKS_API_KEY:
        st.warning(
            "Chưa cấu hình API key. Thêm `NEARBLOCKS_API_KEY` vào Streamlit Secrets rồi tải lại app. "
            "Không dán API key trực tiếp vào mã nguồn hoặc GitHub."
        )
        st.markdown("[Tài liệu NearBlocks API](https://api.nearblocks.io/api-docs/)")
        st.info("Khi cấu hình API key, ông có thể tự nạp danh sách tài khoản công khai gợi ý hoặc nhập địa chỉ đã xác minh.")
        return

    # One-click helper: only makes network calls after the user presses the button.
    if st.button("🔎 Tự nạp danh sách ví công khai gợi ý", key="suggest_holder_accounts"):
        try:
            with st.spinner("Đang lấy validator hiện hoạt và xác minh một số tài khoản hệ sinh thái..."):
                suggested = _near_holder_suggested_accounts(10)
            if not suggested:
                st.warning("Không lấy được tài khoản gợi ý. Có thể NEAR RPC đang tạm lỗi.")
            else:
                suggested_text = "# Tài khoản gợi ý; không phải Top 10 holder toàn mạng\n" + "\n".join(
                    f'{item["account"]} | {item["label"]} | {item["type"]}'
                    for item in suggested
                )
                st.session_state["holder_history_accounts"] = suggested_text
                st.session_state["holder_history_accounts_editor"] = suggested_text
                st.success(f"Đã nạp {len(suggested)} tài khoản gợi ý. Ông có thể sửa danh sách trước khi truy vấn lịch sử.")
        except Exception as exc:
            st.error(f"Không tạo được danh sách gợi ý: {exc}")

    st.caption(
        "Danh sách tự nạp gồm tài khoản hệ sinh thái công khai đã kiểm tra qua RPC và validator đang hoạt động. "
        "Validator stake lớn không đồng nghĩa ví cá voi đang mua; dashboard không tự gắn nhãn ví sàn nếu chưa xác minh."
    )

    default_accounts = st.session_state.get(
        "holder_history_accounts",
        "# Bấm nút 'Tự nạp danh sách ví công khai gợi ý' ở phía trên, hoặc nhập địa chỉ đã xác minh.\n"
        "# Mỗi dòng: account.near | tên hiển thị | loại\n"
        "# Loại: whale, exchange, validator, protocol, unknown",
    )
    accounts_text = st.text_area(
        "Danh sách tài khoản cần truy vấn",
        value=default_accounts,
        height=140,
        key="holder_history_accounts_editor",
        help="Nhập tối đa 10 tài khoản để hạn chế số lần gọi API.",
    )
    st.session_state["holder_history_accounts"] = accounts_text
    config_rows = _parse_whale_config(accounts_text)[:10]
    period_days = st.selectbox(
        "Hiển thị lịch sử", [30, 90, 180, 365], index=1,
        format_func=lambda d: f"{d} ngày gần nhất", key="holder_history_period",
    )

    if st.button("📥 Lấy lịch sử balance từ NearBlocks", type="primary", key="load_holder_history"):
        if not config_rows:
            st.warning("Nhập ít nhất một tài khoản theo đúng định dạng rồi thử lại.")
        else:
            results, errors = [], []
            with st.spinner(f"Đang truy vấn lịch sử cho {len(config_rows)} tài khoản..."):
                for item in config_rows:
                    try:
                        hist_df = _nearblocks_account_balance_history(item["account"], NEARBLOCKS_API_KEY)
                        hist_df["account"] = item["account"]
                        hist_df["label"] = item["label"]
                        hist_df["type"] = item["type"]
                        results.append(hist_df)
                    except Exception as exc:
                        errors.append(f'{item["account"]}: {exc}')
            st.session_state["holder_history_results"] = results
            st.session_state["holder_history_errors"] = errors

    results = st.session_state.get("holder_history_results", [])
    errors = st.session_state.get("holder_history_errors", [])
    if errors:
        with st.expander(f"Chi tiết lỗi ({len(errors)})", expanded=not results):
            for error in errors:
                st.write(f"- {error}")
    if not results:
        st.info(
            "Chưa có dữ liệu lịch sử được tải. Bấm nút truy vấn. Nếu API không trả series lịch sử, "
            "dashboard sẽ báo lỗi thay vì tự tạo dữ liệu."
        )
        st.markdown("[NearBlocks API docs](https://api.nearblocks.io/api-docs/) · [NearBlocks Explorer](https://nearblocks.io/)")
        return

    combined = pd.concat(results, ignore_index=True)
    cutoff = combined["date"].max() - pd.Timedelta(days=int(period_days))
    view = combined[combined["date"] >= cutoff].copy()
    if view.empty:
        view = combined.copy()

    fig = go.Figure()
    for account in view["account"].drop_duplicates():
        sub = view[view["account"] == account].sort_values("date")
        fig.add_trace(go.Scatter(
            x=sub["date"], y=sub["balance"], mode="lines", name=str(sub["label"].iloc[0]),
            line={"width": 2},
            customdata=np.column_stack([sub["account"].astype(str), sub["type"].astype(str)]),
            hovertemplate=(
                "%{x|%d/%m/%Y %H:%M UTC}<br>Ví: %{customdata[0]}<br>Loại: %{customdata[1]}"
                "<br>Số dư: %{y:,.2f} NEAR<extra></extra>"
            ),
        ))
    fig.update_layout(
        template="plotly_dark", height=420, margin=dict(l=10, r=10, t=25, b=15),
        hovermode="x unified", yaxis_title="Số dư native NEAR", xaxis_title="Thời gian (UTC)",
        paper_bgcolor="#0e1117", plot_bgcolor="#0e1117",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    )
    st.plotly_chart(fig, use_container_width=True, config={"displaylogo": False})

    summary_rows = []
    for account in view["account"].drop_duplicates():
        sub = view[view["account"] == account].sort_values("date")
        first_balance, last_balance = float(sub["balance"].iloc[0]), float(sub["balance"].iloc[-1])
        delta = last_balance - first_balance
        summary_rows.append({
            "Ví": sub["label"].iloc[0], "Account": account, "Loại": sub["type"].iloc[0],
            "Số dư đầu kỳ (NEAR)": first_balance, "Số dư mới nhất (NEAR)": last_balance,
            "Thay đổi (NEAR)": delta,
            "Thay đổi (%)": delta / first_balance * 100.0 if first_balance else np.nan,
            "Số điểm lịch sử": len(sub), "Mốc mới nhất (UTC)": sub["date"].max(),
        })
    summary = pd.DataFrame(summary_rows).sort_values("Thay đổi (NEAR)", ascending=False)
    k1, k2, k3 = st.columns(3)
    k1.metric("Số ví có lịch sử", f"{len(summary):,}")
    k2.metric("Tổng thay đổi balance theo dõi", f"{summary['Thay đổi (NEAR)'].sum():+,.2f} NEAR")
    k3.metric("Số điểm dữ liệu", f"{len(view):,}")
    st.dataframe(summary, hide_index=True, use_container_width=True)
    st.caption(
        "Số dư tăng/giảm không khẳng định đã mua/bán. Ví sàn phải được xác minh từ nguồn đáng tin cậy. "
        "Lịch sử balance không cho biết giá vốn; Break-even/All-in Cost vẫn chưa được tính."
    )
    st.markdown("[Nguồn dữ liệu: NearBlocks](https://nearblocks.io/) · [Tài liệu API](https://api.nearblocks.io/api-docs/)")


def render_near_monitor():
    st.markdown("---")
    st.header("🟢 NEAR Monitor")
    st.caption(
        "V1 dùng nguồn dữ liệu công khai/có free tier: CoinGecko cho giá & market data, "
        "NEAR RPC cho trạng thái mạng/validator, DeFiLlama cho TVL hệ sinh thái. "
        "Các điểm Radar là công thức minh bạch của dashboard, không phải điểm AI từ bên thứ ba."
    )

    market = near_market_data()
    chart = near_market_chart(90)
    defi = {"chain": {}, "history": pd.DataFrame(columns=["date", "tvl"])}
    network = {"chain_id": "NEAR", "latest_block": None, "validator_count": 0, "total_stake_near": np.nan, "validators": []}
    try:
        defi = near_defillama_data()
    except Exception as e:
        st.warning(f"DeFiLlama tạm lỗi: {e}")
    try:
        network = near_network_data()
    except Exception as e:
        st.warning(f"NEAR RPC tạm lỗi: {e}")
    score = _near_score_components(market, chart, defi, network)
    if market.get("_source") == "Binance fallback":
        st.warning("⚠️ CoinGecko đang 429. Dashboard vẫn chạy bằng Binance fallback; Market Cap/FDV có thể tạm N/A.")
    elif market.get("_source") == "Unavailable":
        st.warning("⚠️ Không lấy được giá NEAR từ CoinGecko hoặc Binance.")

    p = market.get("price", np.nan)
    chain = defi.get("chain", {})
    hist = defi.get("history")
    tvl_now = _near_num(chain.get("tvl"))
    stables = _near_num(chain.get("stablesMcap", chain.get("stablecoinsMcap", chain.get("stablecoinsMcapUsd"))))
    dex_vol = _near_num(chain.get("24hVolume", chain.get("dexsVolume24h", chain.get("dexsVolume"))))
    active = _near_num(chain.get("activeAddresses", chain.get("activeAddresses24h")))
    txs = _near_num(chain.get("transactions24h", chain.get("transactions")))

    # Market Snapshot
    st.subheader("📌 Market Snapshot")
    a, b, c, d, e, f = st.columns(6)
    a.metric("NEAR", f"${p:.4f}" if np.isfinite(p) else "N/A", f"{market.get('change_24h'):+.2f}%" if np.isfinite(_near_num(market.get('change_24h'))) else None)
    b.metric("Market Cap", _near_fmt_usd(market.get("market_cap")))
    c.metric("FDV", _near_fmt_usd(market.get("fdv")))
    d.metric("Volume 24h", _near_fmt_usd(market.get("volume_24h")))
    e.metric("Circulating", _near_fmt_near(market.get("circulating_supply")))
    f.metric("TVL NEAR", _near_fmt_usd(tvl_now))
    st.caption(f"Nguồn market: {market.get('_source', 'N/A')} • TVL/stablecoin: DeFiLlama")

    # Radar
    st.subheader("🧠 NEAR Radar — điểm minh bạch")
    r1, r2, r3, r4, r5 = st.columns(5)
    r1.metric("Momentum", f"{score['momentum']:.0f}/100")
    r2.metric("Capital Flow", f"{score['flow']:.0f}/100")
    r3.metric("Network", f"{score['network']:.0f}/100")
    r4.metric("Ecosystem", f"{score['ecosystem']:.0f}/100")
    r5.metric("Risk", f"{score['risk']:.0f}/100")
    state = "🟢 Tích cực" if score["total"] >= 65 else ("🟡 Trung tính" if score["total"] >= 45 else "🔴 Thận trọng")
    st.info(f"**Radar tổng hợp: {score['total']:.0f}/100 — {state}**. Đây là chỉ báo cấu trúc, không phải dự báo giá.")

    # Price / ecosystem chart
    st.subheader("📈 Smart Bands — giá & xu hướng")
    if chart is not None and not chart.empty:
        plot = chart.copy()
        plot["EMA20"] = plot["price"].ewm(span=20, adjust=False).mean()
        plot["EMA50"] = plot["price"].ewm(span=50, adjust=False).mean()
        plot["EMA200"] = plot["price"].ewm(span=200, adjust=False).mean()
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=plot["date"], y=plot["price"], name="NEAR", mode="lines"))
        for col in ("EMA20", "EMA50", "EMA200"):
            fig.add_trace(go.Scatter(x=plot["date"], y=plot[col], name=col, mode="lines"))
        fig.update_layout(height=420, margin=dict(l=10, r=10, t=20, b=20), hovermode="x unified")
        st.plotly_chart(fig, use_container_width=True)
        q1, q2, q3 = st.columns(3)
        q1.metric("EMA20", f"${score['ema20']:.4f}" if np.isfinite(score['ema20']) else "N/A")
        q2.metric("EMA50", f"${score['ema50']:.4f}" if np.isfinite(score['ema50']) else "N/A")
        q3.metric("EMA200", f"${score['ema200']:.4f}" if np.isfinite(score['ema200']) else "N/A")

    # NEAR volume heatmap — isolated to this page, BTC app.py remains untouched.
    render_near_heatmap()

    # Flow + ecosystem
    f1, f2 = st.columns(2)
    with f1:
        st.subheader("💰 Capital Flow — proxy")
        st.caption("Dùng thay đổi TVL làm proxy dòng vốn hệ sinh thái; không đồng nhất với CEX netflow hay lệnh mua/bán NEAR.")
        st.metric("TVL 7D", f"{score['tvl7']:+.2f}%" if np.isfinite(score['tvl7']) else "N/A")
        st.metric("TVL 30D", f"{score['tvl30']:+.2f}%" if np.isfinite(score['tvl30']) else "N/A")
        if hist is not None and not hist.empty:
            h = hist.copy()
            h["date"] = pd.to_datetime(h["date"], utc=True)
            fig2 = go.Figure(go.Scatter(x=h["date"], y=h["tvl"], mode="lines", name="TVL"))
            fig2.update_layout(height=300, margin=dict(l=10, r=10, t=20, b=20), yaxis_title="USD")
            st.plotly_chart(fig2, use_container_width=True)
    with f2:
        st.subheader("🌐 Network & Ecosystem")
        n1, n2, n3 = st.columns(3)
        n1.metric("Validators", f"{network.get('validator_count', 0):,}")
        n2.metric("Active addr 24h", f"{active:,.0f}" if np.isfinite(active) else "N/A")
        n3.metric("Tx 24h", f"{txs:,.0f}" if np.isfinite(txs) else "N/A")
        st.write(f"**Staked stake:** {_near_fmt_near(network.get('total_stake_near'))}")
        st.write(f"**DEX volume 24h:** {_near_fmt_usd(dex_vol)}")
        if np.isfinite(_near_num(market.get("market_cap"))) and np.isfinite(tvl_now) and tvl_now > 0:
            st.write(f"**MCap / TVL:** {market['market_cap']/tvl_now:.2f}x")
        st.caption(f"Block mới nhất: {network.get('latest_block', 'N/A')}")

    # Holder / positioning: indexed historical balance for configured accounts.
    render_near_holder_history()

    render_whale_monitor()

    # Final analysis
    st.subheader("🧭 Phân tích cuối")
    rows = [
        ["Giá 24h", f"{market.get('change_24h'):+.2f}%" if np.isfinite(_near_num(market.get('change_24h'))) else "N/A", "Momentum ngắn hạn"],
        ["Giá 7D", f"{score['p7']:+.2f}%" if np.isfinite(score['p7']) else "N/A", "Xu hướng trung hạn"],
        ["TVL 7D", f"{score['tvl7']:+.2f}%" if np.isfinite(score['tvl7']) else "N/A", "Proxy dòng vốn hệ sinh thái"],
        ["TVL 30D", f"{score['tvl30']:+.2f}%" if np.isfinite(score['tvl30']) else "N/A", "Độ bền hệ sinh thái"],
        ["Network", f"{score['network']:.0f}/100", "Validator + trạng thái mạng"],
    ]
    st.dataframe(pd.DataFrame(rows, columns=["Chỉ số", "Giá trị", "Cách đọc"]), hide_index=True, use_container_width=True)
    st.caption(
        "Không có 'Break-even/All-in Cost/Diamond Price' trong V1 vì các chỉ số đó cần mô hình cost-basis/holder history mà các nguồn free đang dùng không cung cấp đầy đủ. "
        "Không dùng điểm này để khẳng định cá voi đang mua/bán."
    )



if "near_loaded" not in st.session_state:
    st.session_state["near_loaded"] = False

if not st.session_state["near_loaded"]:
    st.info("NEAR Monitor đang ở chế độ chờ. Chưa gọi CoinGecko, NEAR RPC hay DeFiLlama.")
    if st.button("🚀 BẮT ĐẦU TẢI DỮ LIỆU NEAR", type="primary", use_container_width=True):
        st.session_state["near_loaded"] = True
        st.rerun()
else:
    top_left, top_right = st.columns([1, 1])
    with top_left:
        if st.button("🔄 Tải lại dữ liệu NEAR", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
    with top_right:
        if st.button("⏸️ Tạm dừng / không tải NEAR", use_container_width=True):
            st.session_state["near_loaded"] = False
            st.rerun()
    render_near_monitor()
