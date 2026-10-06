import requests
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
def near_market_data():
    url = f"https://api.coingecko.com/api/v3/coins/{NEAR_CG_ID}"
    params = {
        "localization": "false",
        "tickers": "false",
        "market_data": "true",
        "community_data": "false",
        "developer_data": "false",
        "sparkline": "false",
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


@st.cache_data(ttl=900, show_spinner=False)
def near_market_chart(days=90):
    url = f"https://api.coingecko.com/api/v3/coins/{NEAR_CG_ID}/market_chart"
    r = requests.get(url, params={"vs_currency": "usd", "days": days}, timeout=25)
    r.raise_for_status()
    prices = r.json().get("prices", [])
    rows = []
    for ts, price in prices:
        try:
            rows.append({
                "date": pd.to_datetime(ts, unit="ms", utc=True),
                "price": float(price),
            })
        except Exception:
            continue
    return pd.DataFrame(rows).drop_duplicates("date").sort_values("date")


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


# =========================
# NEAR WHALE MONITOR V2
# =========================
# Native NEAR không có endpoint "top holders" công khai tương đương FT holders.
# Vì vậy V2 dùng danh sách ví cá voi do người dùng theo dõi + FastNear RPC.
# Đây là cách an toàn hơn việc bịa một "whale flow" từ dữ liệu không đủ.
WHALE_HISTORY_FILE = "near_whale_history.json"
WHALE_WATCHLIST_FILE = "near_whale_watchlist.json"
FASTNEAR_RPC_URL = "https://rpc.mainnet.fastnear.com"


def _whale_load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = __import__("json").load(f)
        return data
    except Exception:
        return default


def _whale_save_json(path, data):
    import json
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    import os
    os.replace(tmp, path)


def _whale_default_watchlist():
    return [
        # account_id, label, category
        # Để trống mặc định: không tự gán ví protocol/exchange thành whale.
    ]


def _whale_normalize_watchlist(rows):
    out = []
    seen = set()
    for row in rows or []:
        if isinstance(row, str):
            parts = [x.strip() for x in row.split("|")]
            account = parts[0] if parts else ""
            label = parts[1] if len(parts) > 1 and parts[1] else account
            category = parts[2].lower() if len(parts) > 2 and parts[2] else "whale"
        elif isinstance(row, dict):
            account = str(row.get("account", "")).strip()
            label = str(row.get("label", "")).strip() or account
            category = str(row.get("category", "whale")).strip().lower()
        else:
            continue

        if not account or account in seen:
            continue
        if category not in {"whale", "exchange", "validator", "protocol", "unknown"}:
            category = "unknown"
        seen.add(account)
        out.append({"account": account, "label": label, "category": category})
    return out


def _whale_rpc_account(account_id):
    payload = {
        "jsonrpc": "2.0",
        "id": "near-whale-monitor",
        "method": "query",
        "params": {
            "request_type": "view_account",
            "finality": "final",
            "account_id": account_id,
        },
    }
    r = requests.post(
        FASTNEAR_RPC_URL,
        json=payload,
        headers={"content-type": "application/json"},
        timeout=20,
    )
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(j["error"])
    result = j.get("result") or {}
    # amount = liquid/unstaked balance; locked = validator stake.
    amount = _near_num(result.get("amount"), 0.0) / 1e24
    locked = _near_num(result.get("locked"), 0.0) / 1e24
    return {
        "balance_near": amount,
        "locked_near": locked,
        "total_near": amount + locked,
        "block_height": result.get("block_height"),
    }


@st.cache_data(ttl=300, show_spinner=False)
def near_whale_balances(accounts_tuple):
    results = []
    for account in accounts_tuple:
        try:
            state = _whale_rpc_account(account)
            results.append({"account": account, **state, "error": ""})
        except Exception as e:
            results.append({
                "account": account,
                "balance_near": np.nan,
                "locked_near": np.nan,
                "total_near": np.nan,
                "block_height": None,
                "error": str(e),
            })
    return pd.DataFrame(results)


def _whale_class_from_balance(balance):
    if not np.isfinite(_near_num(balance)):
        return "N/A"
    b = float(balance)
    if b >= 10_000_000:
        return "Mega Whale"
    if b >= 1_000_000:
        return "Whale"
    if b >= 100_000:
        return "Large"
    if b >= 10_000:
        return "Medium"
    return "Small"


def _whale_read_history():
    data = _whale_load_json(WHALE_HISTORY_FILE, [])
    return data if isinstance(data, list) else []


def _whale_write_snapshot(watchlist, balances_df):
    import datetime as _dt
    history = _whale_read_history()
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()

    snapshot = {
        "timestamp": now,
        "accounts": [],
    }

    balance_map = {}
    for _, row in balances_df.iterrows():
        account = str(row.get("account", ""))
        total = _near_num(row.get("total_near"), np.nan)
        item = next((x for x in watchlist if x["account"] == account), None)
        if item is None:
            continue
        snapshot["accounts"].append({
            "account": account,
            "label": item["label"],
            "category": item["category"],
            "balance_near": None if not np.isfinite(total) else float(total),
            "locked_near": None if not np.isfinite(_near_num(row.get("locked_near"), np.nan)) else float(row["locked_near"]),
        })
        if np.isfinite(total):
            balance_map[account] = float(total)

    # Keep only one snapshot per UTC day. A second manual update on the same
    # day replaces the previous snapshot rather than creating fake daily flow.
    day = now[:10]
    history = [x for x in history if str(x.get("timestamp", ""))[:10] != day]
    snapshot["timestamp"] = now
    history.append(snapshot)
    history = sorted(history, key=lambda x: x.get("timestamp", ""))[-400:]
    _whale_save_json(WHALE_HISTORY_FILE, history)
    return history


def _whale_history_df(history, watchlist):
    if not history:
        return pd.DataFrame()

    rows = []
    label_map = {x["account"]: x for x in watchlist}
    for snap in history:
        date = str(snap.get("timestamp", ""))[:10]
        for item in snap.get("accounts", []):
            account = item.get("account")
            if account not in label_map:
                continue
            bal = item.get("balance_near")
            if bal is None:
                continue
            meta = label_map[account]
            rows.append({
                "date": date,
                "account": account,
                "label": meta["label"],
                "category": meta["category"],
                "balance_near": float(bal),
            })
    return pd.DataFrame(rows)


def _whale_build_summary(watchlist, balances_df, history):
    if balances_df is None or balances_df.empty:
        return pd.DataFrame(), {}

    current = balances_df.copy()
    meta = {x["account"]: x for x in watchlist}
    current["label"] = current["account"].map(lambda x: meta.get(x, {}).get("label", x))
    current["category"] = current["account"].map(lambda x: meta.get(x, {}).get("category", "unknown"))
    current["tier"] = current["total_near"].map(_whale_class_from_balance)

    hist_df = _whale_history_df(history, watchlist)
    prev_map = {}
    if not hist_df.empty:
        dates = sorted(hist_df["date"].dropna().unique())
        if len(dates) >= 2:
            prev_date = dates[-2]
            prev = hist_df[hist_df["date"] == prev_date]
            prev_map = dict(zip(prev["account"], prev["balance_near"]))

    current["prev_near"] = current["account"].map(prev_map)
    current["change_near"] = current["total_near"] - current["prev_near"]
    current["change_pct"] = np.where(
        current["prev_near"].notna() & (current["prev_near"] > 0),
        current["change_near"] / current["prev_near"] * 100,
        np.nan,
    )

    whale = current[current["category"] == "whale"].copy()
    exchanges = current[current["category"] == "exchange"].copy()

    whale_net = float(whale["change_near"].sum(min_count=1)) if not whale.empty else np.nan
    exchange_net = float(exchanges["change_near"].sum(min_count=1)) if not exchanges.empty else np.nan

    whale_total = float(whale["total_near"].sum()) if not whale.empty else np.nan
    whale_pct = whale_net / whale_total * 100 if np.isfinite(whale_net) and whale_total > 0 else np.nan

    positive = int((whale["change_near"] > 0).sum()) if not whale.empty else 0
    negative = int((whale["change_near"] < 0).sum()) if not whale.empty else 0

    # Transparent score: balance change only. It is NOT a buy/sell detector.
    score = float(np.clip(50 + (whale_pct if np.isfinite(whale_pct) else 0) * 15, 0, 100))
    if score >= 65:
        state = "🟢 Tích lũy"
    elif score >= 45:
        state = "🟡 Trung tính"
    else:
        state = "🔴 Phân phối"

    summary = {
        "whale_net": whale_net,
        "exchange_net": exchange_net,
        "whale_total": whale_total,
        "whale_pct": whale_pct,
        "positive": positive,
        "negative": negative,
        "score": score,
        "state": state,
    }
    return current, summary


def _whale_flow_chart(history, watchlist):
    hist_df = _whale_history_df(history, watchlist)
    if hist_df.empty:
        return pd.DataFrame()

    whale_df = hist_df[hist_df["category"] == "whale"].copy()
    if whale_df.empty:
        return pd.DataFrame()

    daily = whale_df.groupby("date", as_index=False)["balance_near"].sum()
    daily = daily.sort_values("date")
    daily["net_change"] = daily["balance_near"].diff()
    return daily


def render_near_whale_monitor():
    st.markdown("---")
    st.header("🐋 NEAR Whale Monitor V2")
    st.caption(
        "Theo dõi thay đổi số dư native NEAR của các ví ông đánh dấu là whale. "
        "Dương = số dư tăng (tích lũy), âm = số dư giảm (phân phối). "
        "Không đồng nhất với mua/bán spot."
    )

    saved = _whale_normalize_watchlist(
        _whale_load_json(WHALE_WATCHLIST_FILE, _whale_default_watchlist())
    )

    with st.expander("⚙️ Cấu hình ví theo dõi", expanded=not bool(saved)):
        st.write(
            "Mỗi dòng: `account.near | tên hiển thị | loại`. "
            "Loại hợp lệ: `whale`, `exchange`, `validator`, `protocol`, `unknown`."
        )
        st.caption(
            "Ví `whale` mới được tính vào Whale Score. Ví `exchange` được tách riêng. "
            "Không nên đánh dấu ví sàn/validator là whale."
        )
        default_text = "\n".join(
            f"{x['account']} | {x['label']} | {x['category']}" for x in saved
        )
        watch_text = st.text_area(
            "Danh sách ví",
            value=default_text,
            height=180,
            placeholder="vi_du.near | Whale #1 | whale",
        )
        if st.button("💾 Lưu danh sách ví", key="save_whale_watchlist"):
            rows = []
            for line in watch_text.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    rows.append(line)
            normalized = _whale_normalize_watchlist(rows)
            _whale_save_json(WHALE_WATCHLIST_FILE, normalized)
            st.success(f"Đã lưu {len(normalized)} ví.")
            st.rerun()

    watchlist = _whale_normalize_watchlist(
        _whale_load_json(WHALE_WATCHLIST_FILE, saved)
    )

    if not watchlist:
        st.info(
            "Chưa có ví theo dõi. Ông nhập danh sách whale ở phần cấu hình rồi bấm Lưu. "
            "Native NEAR hiện không có public free endpoint đáng tin cậy để tự lấy rich-list Top 100."
        )
        return

    accounts = tuple(x["account"] for x in watchlist)
    c1, c2 = st.columns([1, 1])
    with c1:
        load_now = st.button(
            "🔍 LẤY SỐ DƯ WHALE",
            type="primary",
            use_container_width=True,
            key="load_whale_balances",
        )
    with c2:
        if st.button(
            "🧹 Xóa lịch sử whale",
            use_container_width=True,
            key="clear_whale_history",
        ):
            _whale_save_json(WHALE_HISTORY_FILE, [])
            st.success("Đã xóa lịch sử whale.")
            st.rerun()

    # Reading balances is only performed after the explicit button.
    if not load_now:
        history = _whale_read_history()
        if history:
            st.info(
                f"Đã có {len(history)} snapshot. Bấm **LẤY SỐ DƯ WHALE** để cập nhật snapshot hôm nay."
            )
        else:
            st.info("Chưa gọi RPC. Bấm **LẤY SỐ DƯ WHALE** để bắt đầu.")
        return

    with st.spinner(f"Đang đọc {len(accounts)} ví từ FastNear RPC..."):
        balances = near_whale_balances(accounts)

    history = _whale_write_snapshot(watchlist, balances)
    current, summary = _whale_build_summary(watchlist, balances, history)

    if current.empty:
        st.warning("Không đọc được số dư ví.")
        return

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Whale netflow", _near_fmt_near(summary["whale_net"]))
    m2.metric("Whale holdings", _near_fmt_near(summary["whale_total"]))
    m3.metric("Whale score", f"{summary['score']:.0f}/100")
    m4.metric("Đang tăng", f"{summary['positive']} ví")
    m5.metric("Đang giảm", f"{summary['negative']} ví")

    st.info(
        f"**{summary['state']}** — Whale netflow: "
        f"{_near_fmt_near(summary['whale_net'])}. "
        "Đây là thay đổi số dư ví, chưa phải xác nhận cá voi mua/bán."
    )

    if np.isfinite(summary["exchange_net"]):
        ex_state = (
            "rút khỏi các ví exchange đã theo dõi"
            if summary["exchange_net"] < 0
            else "tăng số dư ở các ví exchange đã theo dõi"
        )
        st.caption(
            f"Exchange netflow (chỉ các ví ông đánh dấu): "
            f"{_near_fmt_near(summary['exchange_net'])} → {ex_state}."
        )

    # Current whale table
    st.subheader("📋 Danh sách ví")
    display = current[
        ["label", "account", "category", "tier", "total_near", "change_near", "change_pct", "block_height", "error"]
    ].copy()
    display.columns = [
        "Ví", "Account", "Loại", "Tier", "Số dư NEAR",
        "Thay đổi NEAR", "Thay đổi %", "Block", "Lỗi"
    ]
    st.dataframe(
        display.sort_values("Số dư NEAR", ascending=False),
        hide_index=True,
        use_container_width=True,
        column_config={
            "Số dư NEAR": st.column_config.NumberColumn(format="%,.0f"),
            "Thay đổi NEAR": st.column_config.NumberColumn(format="%,.0f"),
            "Thay đổi %": st.column_config.NumberColumn(format="%.2f%%"),
        },
    )

    # Historical whale holdings and daily netflow
    flow = _whale_flow_chart(history, watchlist)
    if not flow.empty:
        st.subheader("📈 Whale Holdings & Netflow")
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=flow["date"],
                y=flow["balance_near"],
                name="Whale holdings",
                mode="lines+markers",
            )
        )
        fig.update_layout(
            height=360,
            margin=dict(l=10, r=10, t=20, b=20),
            hovermode="x unified",
            yaxis_title="NEAR",
        )
        st.plotly_chart(fig, use_container_width=True)

        fig2 = go.Figure()
        fig2.add_trace(
            go.Bar(
                x=flow["date"],
                y=flow["net_change"],
                name="Daily netflow",
            )
        )
        fig2.add_hline(y=0, line_width=1)
        fig2.update_layout(
            height=300,
            margin=dict(l=10, r=10, t=20, b=20),
            hovermode="x unified",
            yaxis_title="NEAR / day",
        )
        st.plotly_chart(fig2, use_container_width=True)

        st.caption(
            "Lịch sử chỉ có ý nghĩa từ ngày đầu tiên ông bắt đầu snapshot. "
            "Nên lấy ít nhất 7–14 ngày để đọc xu hướng, tốt nhất 30–90 ngày."
        )

    st.subheader("🧠 Cách đọc tín hiệu")
    read_rows = [
        ["Whale holdings ↑", "Tích lũy số dư", "Tích cực", "Chưa đủ để kết luận mua spot"],
        ["Whale holdings ↓", "Phân phối số dư", "Tiêu cực", "Có thể là chuyển ví/staking/chi tiêu"],
        ["Whale ↑ + Exchange ↓", "Whale tăng, exchange giảm", "Thiên tích lũy", "Cần kiểm tra ví cụ thể"],
        ["Whale ↓ + Exchange ↑", "Whale giảm, exchange tăng", "Thiên phân phối", "Không phải lúc nào cũng là bán"],
    ]
    st.dataframe(
        pd.DataFrame(read_rows, columns=["Tín hiệu", "Ý nghĩa", "Bias", "Lưu ý"]),
        hide_index=True,
        use_container_width=True,
    )

    st.caption(
        "Powered by FastNear RPC for native account state. "
        "FastNear documents view_account as the canonical account-balance query. "
        "Whale Monitor does not claim to reproduce Nansen's proprietary labeling."
    )


def render_near_monitor():
    st.markdown("---")
    st.header("🟢 NEAR Monitor")
    st.caption(
        "V1 dùng nguồn dữ liệu công khai/có free tier: CoinGecko cho giá & market data, "
        "NEAR RPC cho trạng thái mạng/validator, DeFiLlama cho TVL hệ sinh thái. "
        "Các điểm Radar là công thức minh bạch của dashboard, không phải điểm AI từ bên thứ ba."
    )

    try:
        market = near_market_data()
        chart = near_market_chart(90)
        defi = near_defillama_data()
        network = near_network_data()
        score = _near_score_components(market, chart, defi, network)
    except Exception as e:
        st.warning(f"NEAR Monitor chưa lấy đủ dữ liệu: {e}")
        return

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
    st.caption("Nguồn market: CoinGecko • TVL/stablecoin: DeFiLlama")

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
    render_near_whale_monitor()
