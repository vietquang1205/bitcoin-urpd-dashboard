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
    st.caption(f"Nguồn market: {market.get("_source", "N/A")} • TVL/stablecoin: DeFiLlama")

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

    # Holder / positioning
    st.subheader("🐋 Holder Flow & Investor Positioning")
    st.info(
        "V1 **không giả lập** cost-basis cohort, Break-even, All-in Cost hay whale accumulation. "
        "Các chỉ số đó cần lịch sử holder/balance đủ dài và cách gán giá vốn cho từng holder. "
        "Nguồn free hiện dùng cho V1 chưa đủ để tính đáng tin cậy cho native NEAR. "
        "Vì vậy phần này chỉ giữ trạng thái dữ liệu thay vì bịa số liệu."
    )
    if NEARBLOCKS_API_KEY:
        st.caption("NEARBLOCKS_API_KEY đã có trong Secrets; có thể mở rộng V2 sang holder history/native holder flow khi endpoint phù hợp được cấu hình.")
    else:
        st.caption("Nếu cần V2 holder flow thật, có thể thêm NEARBLOCKS_API_KEY vào Secrets; NearBlocks hiện có free tier nhưng giới hạn request và yêu cầu attribution.")

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
