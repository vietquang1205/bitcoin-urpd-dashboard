"""
NEAR Monitor V2 — trang riêng (pages/NEAR_Monitor.py) theo cùng kiến trúc với BTC URPD Monitor.

Hai chế độ chạy:
  • Dashboard:          Streamlit tự chạy file này khi mở trang "NEAR Monitor".
                        CHỈ ĐỌC near_history.json (+ giá trực tiếp), không gọi hàng loạt API.
  • Cập nhật dữ liệu:   python pages/NEAR_Monitor.py --update-data   (GitHub Actions gọi mỗi ngày)
                        Lấy giá/khối lượng (CoinGecko), staking + số dư ví (NEAR RPC),
                        TVL/stablecoin/DEX (DeFiLlama), phái sinh (OKX, dự phòng Hyperliquid),
                        hoạt động mạng (NearBlocks — chỉ khi có NEARBLOCKS_API_KEY).

Danh sách ví theo dõi: file near_watchlist.txt ở thư mục gốc repo, mỗi dòng
    account.near | Tên hiển thị | loại        (loại: whale, exchange, validator, protocol, unknown)
Nếu chưa có file, updater tự theo dõi 20 validator pool lớn nhất.

Lưu ý: Binance.com chặn máy chủ ở Mỹ (lỗi 451) — Streamlit Cloud và GitHub Actions đều ở Mỹ,
nên bản này dùng Coinbase/Kraken/OKX thay cho Binance.
"""
import os
import re
import sys
import json
import time
from datetime import datetime, timedelta, timezone

import requests
import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots

NEAR_VERSION = "V2"
UPDATE_MODE = "--update-data" in sys.argv

NEAR_FILE = "near_history.json"
WATCHLIST_FILE = "near_watchlist.txt"
BTC_PRICE_FILE = "bgeometrics_realized_pl_history.json"   # dùng lại giá BTC của app BTC để tính NEAR/BTC
NEAR_CG_ID = "near"
RPC_URLS = ["https://rpc.mainnet.fastnear.com", "https://free.rpc.fastnear.com", "https://rpc.mainnet.near.org"]
YOCTO = 1e24
HISTORY_DAYS = 730
VN_TZ = timezone(timedelta(hours=7))
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 NEAR-Monitor/2", "Accept": "application/json"}
VALID_TYPES = {"whale", "exchange", "validator", "protocol", "unknown"}


# =============================================================================
# TIỆN ÍCH
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


def safe_float(x):
    try:
        v = float(x)
        return v if np.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def fmt_price(x):
    if x is None or not np.isfinite(x):
        return "N/A"
    return f"${x:,.4f}" if abs(x) < 10 else f"${x:,.2f}"


def fmt_big_usd(x, signed=False):
    if x is None or not np.isfinite(x):
        return "N/A"
    sign = ("+" if x >= 0 else "−") if signed else ("−" if x < 0 else "")
    a = abs(x)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{sign}${a / div:.2f}{suf}"
    return f"{sign}${a:,.0f}"


def fmt_near(x, signed=False):
    if x is None or not np.isfinite(x):
        return "N/A"
    a = abs(x)
    sign = ("+" if x >= 0 else "−") if signed else ("−" if x < 0 else "")
    if a >= 1e9:
        return f"{sign}{a / 1e9:.2f}B Ⓝ"
    if a >= 1e6:
        return f"{sign}{a / 1e6:.2f}M Ⓝ"
    return f"{sign}{a:,.0f} Ⓝ"


def clean_secret(raw):
    """Lấy đúng giá trị kể cả khi lỡ dán 'TÊN = "giá trị"' hoặc nhiều dòng."""
    for line in str(raw or "").strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^[A-Za-z_][A-Za-z0-9_]*\s*=\s*(.+)$', line)
        return (m.group(1) if m else line).strip().strip('"').strip("'").strip()
    return ""


def redact(msg):
    text = str(msg)
    for name in ("NEARBLOCKS_API_KEY", "COINGECKO_API_KEY", "GITHUB_TOKEN"):
        v = clean_secret(os.environ.get(name, ""))
        if v and len(v) >= 6:
            text = text.replace(v, "***")
    text = re.sub(r"(?i)(header value:).*", r"\1 ***", text, flags=re.S)
    return re.sub(r"github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9]{20,}", "***", text)


def _get(url, params=None, headers=None, timeout=25, retries=2):
    """GET có thử lại khi bị giới hạn (429) hoặc lỗi mạng tạm thời."""
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers={**HTTP_HEADERS, **(headers or {})}, timeout=timeout)
            if r.status_code == 429 and attempt < retries:
                time.sleep(15 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(3)
    raise RuntimeError(redact(last))


def _to_date(ts):
    """ms / s / ISO → date (UTC)."""
    if ts is None:
        return None
    try:
        x = float(ts)
        if x > 1e11:
            x /= 1000
        return datetime.fromtimestamp(x, tz=timezone.utc).date()
    except (TypeError, ValueError):
        pass
    try:
        return pd.to_datetime(ts, utc=True).date()
    except Exception:
        return None


# =============================================================================
# NGUỒN DỮ LIỆU
# =============================================================================
def _cg_headers():
    key = clean_secret(os.environ.get("COINGECKO_API_KEY", ""))
    return {"x-cg-demo-api-key": key} if key else {}


def fetch_coingecko_history(days):
    """Giá, vốn hóa và tổng khối lượng (mọi sàn) theo ngày."""
    j = _get(f"https://api.coingecko.com/api/v3/coins/{NEAR_CG_ID}/market_chart",
             params={"vs_currency": "usd", "days": int(days), "interval": "daily"}, headers=_cg_headers())
    cols = {}
    for key, name in (("prices", "price"), ("market_caps", "market_cap"), ("total_volumes", "volume_usd")):
        for ts, v in j.get(key, []) or []:
            d = _to_date(ts)
            if d is not None and safe_float(v) is not None:
                cols.setdefault(d, {})[name] = float(v)
    return cols


def fetch_coingecko_now():
    j = _get(f"https://api.coingecko.com/api/v3/coins/{NEAR_CG_ID}",
             params={"localization": "false", "tickers": "false", "market_data": "true",
                     "community_data": "false", "developer_data": "false", "sparkline": "false"},
             headers=_cg_headers(), retries=0, timeout=15)
    md = j.get("market_data", {})
    return {
        "price": safe_float(md.get("current_price", {}).get("usd")),
        "change_24h": safe_float(md.get("price_change_percentage_24h")),
        "market_cap": safe_float(md.get("market_cap", {}).get("usd")),
        "fdv": safe_float(md.get("fully_diluted_valuation", {}).get("usd")),
        "volume_24h": safe_float(md.get("total_volume", {}).get("usd")),
        "circulating": safe_float(md.get("circulating_supply")),
        "total_supply": safe_float(md.get("total_supply")),
        "source": "CoinGecko",
    }


def fetch_coinbase_now():
    j = _get("https://api.exchange.coinbase.com/products/NEAR-USD/stats", retries=0, timeout=15)
    last, open_ = safe_float(j.get("last")), safe_float(j.get("open"))
    return {"price": last, "change_24h": (last / open_ - 1) * 100 if last and open_ else None,
            "volume_24h": (safe_float(j.get("volume")) or 0) * (last or 0) or None, "source": "Coinbase"}


def fetch_candles(days=60):
    """Nến 4–6 giờ cho volume profile ngắn hạn: Coinbase → Kraken → Binance (data-api)."""
    errors = []
    try:  # Coinbase: tối đa 300 nến/lần; 6 giờ × 300 = 75 ngày
        end = utc_now()
        start = end - timedelta(days=min(int(days), 74))
        rows = _get("https://api.exchange.coinbase.com/products/NEAR-USD/candles",
                    params={"granularity": 21600, "start": start.isoformat(), "end": end.isoformat()}, retries=1)
        df = pd.DataFrame(rows, columns=["t", "low", "high", "open", "close", "volume"])
        if not df.empty:
            df["time"] = pd.to_datetime(df["t"], unit="s", utc=True)
            df["quote_volume"] = df["volume"] * df["close"]
            return df.sort_values("time").reset_index(drop=True), "Coinbase (nến 6 giờ)"
    except Exception as e:
        errors.append(f"Coinbase: {e}")
    try:  # Kraken: nến 4 giờ, tối đa 720
        j = _get("https://api.kraken.com/0/public/OHLC", params={"pair": "NEARUSD", "interval": 240}, retries=1)
        res = j.get("result", {})
        key = next((k for k in res if k != "last"), None)
        df = pd.DataFrame(res.get(key, []), columns=["t", "open", "high", "low", "close", "vwap", "volume", "count"])
        if not df.empty:
            for c in ("open", "high", "low", "close", "volume"):
                df[c] = df[c].astype(float)
            df["time"] = pd.to_datetime(df["t"], unit="s", utc=True)
            df["quote_volume"] = df["volume"] * df["close"]
            df = df[df["time"] >= utc_now() - timedelta(days=int(days))]
            return df.sort_values("time").reset_index(drop=True), "Kraken (nến 4 giờ)"
    except Exception as e:
        errors.append(f"Kraken: {e}")
    try:
        rows = _get("https://data-api.binance.vision/api/v3/klines",
                    params={"symbol": "NEARUSDT", "interval": "4h", "limit": min(1000, int(days) * 6)}, retries=1)
        df = pd.DataFrame([{"time": pd.to_datetime(r[0], unit="ms", utc=True), "open": float(r[1]),
                            "high": float(r[2]), "low": float(r[3]), "close": float(r[4]),
                            "quote_volume": float(r[7])} for r in rows])
        if not df.empty:
            return df, "Binance (nến 4 giờ)"
    except Exception as e:
        errors.append(f"Binance: {e}")
    raise RuntimeError(" | ".join(errors))


def near_rpc(method, params):
    last = None
    for url in RPC_URLS:
        try:
            r = requests.post(url, json={"jsonrpc": "2.0", "id": "near-monitor", "method": method, "params": params},
                              headers={"content-type": "application/json"}, timeout=25)
            r.raise_for_status()
            j = r.json()
            if j.get("error"):
                raise RuntimeError(str(j["error"])[:200])
            return j.get("result", {})
        except Exception as e:
            last = e
    raise RuntimeError(f"NEAR RPC lỗi: {last}")


def fetch_staking():
    block = near_rpc("block", {"finality": "final"})
    header = block.get("header", {})
    vals = near_rpc("validators", [None])
    current = vals.get("current_validators", []) or []
    stakes = sorted(((v.get("account_id"), (safe_float(v.get("stake")) or 0) / YOCTO) for v in current),
                    key=lambda x: -x[1])
    total_stake = sum(s for _, s in stakes)
    total_supply = (safe_float(header.get("total_supply")) or 0) / YOCTO
    top10 = sum(s for _, s in stakes[:10])
    return {
        "total_supply": total_supply or None, "total_stake": total_stake or None,
        "staking_ratio": total_stake / total_supply * 100 if total_supply else None,
        "validators": len(stakes), "top10_stake_share": top10 / total_stake * 100 if total_stake else None,
        "epoch_height": vals.get("epoch_height"), "block_height": header.get("height"),
        "top_validators": [a for a, _ in stakes[:20] if a],
    }


def fetch_account(account):
    res = near_rpc("query", {"request_type": "view_account", "finality": "final", "account_id": account.strip()})
    liquid = (safe_float(res.get("amount")) or 0) / YOCTO
    locked = (safe_float(res.get("locked")) or 0) / YOCTO
    return {"balance": liquid + locked, "liquid": liquid, "locked": locked}


def fetch_defillama():
    """TVL, stablecoin và khối lượng DEX của chain NEAR (toàn bộ lịch sử)."""
    out, errors = {}, []
    try:
        for row in _get("https://api.llama.fi/v2/historicalChainTvl/Near"):
            d = _to_date(row.get("date"))
            if d and safe_float(row.get("tvl")) is not None:
                out.setdefault(d, {})["tvl"] = float(row["tvl"])
    except Exception as e:
        errors.append(f"TVL: {e}")
    try:
        for row in _get("https://stablecoins.llama.fi/stablecoincharts/Near"):
            d = _to_date(row.get("date"))
            circ = row.get("totalCirculatingUSD") or row.get("totalCirculating") or {}
            v = sum(safe_float(x) or 0 for x in circ.values()) if isinstance(circ, dict) else safe_float(circ)
            if d and v:
                out.setdefault(d, {})["stables"] = float(v)
    except Exception as e:
        errors.append(f"Stablecoin: {e}")
    try:
        j = _get("https://api.llama.fi/overview/dexs/near",
                 params={"excludeTotalDataChart": "false", "excludeTotalDataChartBreakdown": "true",
                         "dataType": "dailyVolume"})
        for ts, v in j.get("totalDataChart", []) or []:
            d = _to_date(ts)
            if d and safe_float(v) is not None:
                out.setdefault(d, {})["dex_volume"] = float(v)
    except Exception as e:
        errors.append(f"DEX: {e}")
    return out, errors


def fetch_derivatives():
    """Phái sinh NEAR: OKX (OI, khối lượng, long/short, funding) → dự phòng Hyperliquid."""
    out, errors, now = {}, [], {}
    okx = "https://www.okx.com/api/v5"
    try:
        j = _get(f"{okx}/rubik/stat/contracts/open-interest-volume", params={"ccy": "NEAR", "period": "1D"})
        for row in j.get("data", []) or []:
            d = _to_date(row[0])
            if d:
                out.setdefault(d, {}).update({"oi_usd": safe_float(row[1]), "perp_volume_usd": safe_float(row[2])})
    except Exception as e:
        errors.append(f"OKX OI: {e}")
    try:
        j = _get(f"{okx}/rubik/stat/contracts/long-short-account-ratio", params={"ccy": "NEAR", "period": "1D"})
        for row in j.get("data", []) or []:
            d = _to_date(row[0])
            if d and safe_float(row[1]) is not None:
                out.setdefault(d, {})["long_short_ratio"] = float(row[1])
    except Exception as e:
        errors.append(f"OKX long/short: {e}")
    try:
        j = _get(f"{okx}/public/funding-rate-history", params={"instId": "NEAR-USDT-SWAP", "limit": 100})
        rows = [(int(r["fundingTime"]), safe_float(r.get("realizedRate") or r.get("fundingRate")))
                for r in j.get("data", []) or [] if r.get("fundingTime")]
        rows = sorted((t, v) for t, v in rows if v is not None)
        if len(rows) >= 2:
            gaps = np.diff([t for t, _ in rows]) / 3.6e6
            interval_h = float(np.median(gaps)) or 8.0
            per_year = 24 * 365 / interval_h
            daily = {}
            for t, v in rows:
                daily.setdefault(_to_date(t), []).append(v)
            for d, vs in daily.items():
                out.setdefault(d, {})["funding_apr"] = float(np.mean(vs)) * per_year * 100
            now["funding_interval_h"] = interval_h
        now["funding_source"] = "OKX"
    except Exception as e:
        errors.append(f"OKX funding: {e}")
    try:  # ảnh chụp hiện tại từ Hyperliquid (funding theo giờ, OI theo số coin)
        r = requests.post("https://api.hyperliquid.xyz/info", json={"type": "metaAndAssetCtxs"}, timeout=20)
        r.raise_for_status()
        meta, ctxs = r.json()
        names = [u.get("name") for u in meta.get("universe", [])]
        if "NEAR" in names:
            c = ctxs[names.index("NEAR")]
            mark = safe_float(c.get("markPx"))
            now.update({"hl_funding_apr": (safe_float(c.get("funding")) or 0) * 24 * 365 * 100,
                        "hl_oi_usd": (safe_float(c.get("openInterest")) or 0) * (mark or 0),
                        "hl_volume_usd": safe_float(c.get("dayNtlVlm"))})
    except Exception as e:
        errors.append(f"Hyperliquid: {e}")
    return out, now, errors


def fetch_nearblocks(api_key):
    """Hoạt động mạng theo ngày (cần NEARBLOCKS_API_KEY). Đọc linh hoạt vì API không công bố schema."""
    out, errors = {}, []
    headers = {"Authorization": f"Bearer {api_key}"}
    for path, mapping in (("/v3/txn-stats", {"txns": ("txn", "txns", "count")}),
                          ("/v3/address-stats", {"active_accounts": ("active", "addresses", "count")})):
        try:
            j = _get("https://api.nearblocks.io" + path, headers=headers, retries=1)
            rows = None
            stack = [j]
            while stack and rows is None:
                node = stack.pop()
                if isinstance(node, list) and node and isinstance(node[0], dict) and "date" in node[0]:
                    rows = node
                elif isinstance(node, dict):
                    stack.extend(node.values())
            for row in rows or []:
                d = _to_date(row.get("date"))
                if not d:
                    continue
                for name, hints in mapping.items():
                    for k, v in row.items():
                        if k != "date" and any(h in k.lower() for h in hints) and safe_float(v) is not None:
                            out.setdefault(d, {})[name] = float(v)
                            break
        except Exception as e:
            errors.append(f"NearBlocks {path}: {e}")
    return out, errors


# =============================================================================
# MÔ HÌNH GIÁ VỐN ƯỚC TÍNH (tương tự URPD của BTC)
# =============================================================================
# NEAR không có UTXO nên không đo được giá vốn on-chain như BTC. Mô hình này ƯỚC TÍNH:
#   • Mỗi ngày một phần nguồn cung "đổi chủ" = hệ số luân chuyển × (khối lượng / nguồn cung lưu hành).
#   • Phần đổi chủ được chuyển (từ mọi vùng giá cũ, theo tỷ lệ) sang mức giá của ngày hôm đó.
#   • Lượng NEAR mới phát hành (cung tăng) được ghi nhận tại giá ngày phát hành.
#   • Phần cung chưa "đổi chủ" trong cửa sổ dữ liệu = "giá vốn cũ hơn" (không xác định).
# Khối lượng sàn lớn hơn nhiều so với lượng coin thật sự đổi chủ, nên hệ số luân chuyển < 1.
def daily_frame(data):
    daily = data.get("daily", {}) if isinstance(data, dict) else {}
    rows = [{"date": parse_day(d), **rec} for d, rec in daily.items() if is_day_key(d) and isinstance(rec, dict)]
    if not rows:
        return pd.DataFrame(columns=["date"])
    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    for c in df.columns:
        if c != "date":
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def cost_basis_model(df, alpha=0.2, n_bins=160):
    """Trả dict: edges, dist (NEAR theo vùng giá), unknown, snapshots (bins × ngày), dates, prices."""
    need = {"price", "volume_usd", "market_cap"}
    if df is None or df.empty or not need.issubset(df.columns):
        return None
    d = df.dropna(subset=["price", "volume_usd", "market_cap"])
    d = d[(d.price > 0) & (d.market_cap > 0)].tail(730)
    if len(d) < 30:
        return None
    prices = d.price.to_numpy(float)
    supply = (d.market_cap / d.price).to_numpy(float)
    volume = (d.volume_usd / d.price).to_numpy(float)
    top = float(prices.max()) * 1.15
    step = top / n_bins
    edges = np.arange(n_bins + 1) * step
    dist = np.zeros(n_bins)
    unknown = float(supply[0])
    snaps = np.zeros((n_bins, len(d)))
    prev_supply = supply[0]
    for i, (p, s, v) in enumerate(zip(prices, supply, volume)):
        b = min(n_bins - 1, int(p // step))
        f = float(np.clip(alpha * v / max(s, 1.0), 0.0, 0.5))
        moved = dist * f
        dist -= moved
        moved_unknown = unknown * f
        unknown -= moved_unknown
        dist[b] += moved.sum() + moved_unknown
        growth = s - prev_supply
        if growth > 0:          # NEAR mới phát hành / mở khóa → giá vốn tại giá hôm đó
            dist[b] += growth
        elif growth < 0:        # cung lưu hành giảm (đốt/khóa lại) → bớt đều theo tỷ lệ
            total = dist.sum() + unknown
            if total > 0:
                k = max(0.0, 1 + growth / total)
                dist *= k
                unknown *= k
        prev_supply = s
        snaps[:, i] = dist
    return {"edges": edges, "dist": dist, "unknown": unknown, "snaps": snaps,
            "dates": list(d.date), "prices": prices, "step": step}


def model_metrics(model, price):
    if not model or not price:
        return {}
    mids = (model["edges"][:-1] + model["edges"][1:]) / 2
    dist = model["dist"]
    known = float(dist.sum())
    total = known + model["unknown"]
    if known <= 0:
        return {}
    realized = float((dist * mids).sum() / known)
    in_loss = float(dist[mids > price].sum())
    return {"realized_price": realized, "mvrv": price / realized if realized else None,
            "loss_pct": in_loss / known * 100, "known_pct": known / total * 100 if total else None,
            "in_loss": in_loss, "known": known}


def model_series(model):
    """Giá vốn ước tính & % cung lỗ theo từng ngày (từ snapshots)."""
    if not model:
        return pd.DataFrame()
    mids = (model["edges"][:-1] + model["edges"][1:]) / 2
    rows = []
    for i, (d, p) in enumerate(zip(model["dates"], model["prices"])):
        col = model["snaps"][:, i]
        k = col.sum()
        if k > 0:
            rows.append({"date": d, "price": p, "realized_price": float((col * mids).sum() / k),
                         "loss_pct": float(col[mids > p].sum() / k * 100)})
    return pd.DataFrame(rows)


def find_zones(model, price, n=3, window=0.35, zone_bins=3):
    """Cụm giá vốn ước tính dày nhất dưới/trên giá (giống vùng hỗ trợ/kháng cự của trang BTC)."""
    if not model or not price:
        return [], []
    edges, amt = model["edges"], model["dist"]
    if len(amt) < zone_bins:
        return [], []
    sums = np.convolve(amt, np.ones(zone_bins), "valid")
    lows, highs = edges[:len(sums)], edges[:len(sums)] + zone_bins * model["step"]
    known = float(amt.sum()) or 1.0

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
        return [{"low": float(lows[i]), "high": float(highs[i]), "near": float(sums[i]),
                 "share_pct": float(sums[i]) / known * 100,
                 "distance_pct": ((lows[i] + highs[i]) / 2 / price - 1) * 100} for i in chosen]

    sup = pick((highs <= price) & (lows >= price * (1 - window)))
    res = pick((lows >= price) & (highs <= price * (1 + window)))
    return sorted(sup, key=lambda z: -z["high"]), sorted(res, key=lambda z: z["low"])


# =============================================================================
# TÍN HIỆU & TÓM TẮT
# =============================================================================
def _light(v, good_above=None, bad_below=None, good_below=None, bad_above=None):
    if v is None or not np.isfinite(v):
        return "⚪"
    if good_above is not None:
        return "🟢" if v > good_above else ("🔴" if v < bad_below else "🟡")
    return "🟢" if v < good_below else ("🔴" if v > bad_above else "🟡")


def last_val(df, col, before=None):
    if df is None or df.empty or col not in df.columns:
        return None, None
    s = df[["date", col]].dropna()
    if before is not None:
        s = s[s["date"] <= before]
    if s.empty:
        return None, None
    return float(s.iloc[-1][col]), s.iloc[-1]["date"]


def change_pct(df, col, days):
    v, d = last_val(df, col)
    if v is None:
        return None
    old, _ = last_val(df, col, before=d - timedelta(days=days))
    return (v / old - 1) * 100 if old else None


def change_abs(df, col, days):
    v, d = last_val(df, col)
    if v is None:
        return None
    old, _ = last_val(df, col, before=d - timedelta(days=days))
    return v - old if old is not None else None


def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


def build_signals(price, df, mm, mseries, whale_flow7, near_btc_30):
    rows = []
    if mm.get("realized_price"):
        d = (price / mm["realized_price"] - 1) * 100
        rows.append(["Giá so với giá vốn ước tính", _light(d, good_above=5, bad_below=-5),
                     f"{d:+.1f}% (giá vốn ≈ {fmt_price(mm['realized_price'])})",
                     "Trên giá vốn: số đông đang lãi. Dưới: số đông lỗ, nhịp hồi dễ gặp bán hòa vốn."])
    if df is not None and not df.empty and df["price"].notna().sum() >= 60:
        e200 = float(ema(df["price"].dropna(), 200).iloc[-1])
        d = (price / e200 - 1) * 100
        rows.append(["Giá so với EMA200 (xu hướng dài)", _light(d, good_above=0, bad_below=-10),
                     f"{d:+.1f}% (EMA200 {fmt_price(e200)})", "Trên EMA200 = xu hướng dài hạn tăng."])
    if mseries is not None and len(mseries) > 8:
        dl = float(mseries.loss_pct.iloc[-1] - mseries.loss_pct.iloc[-8])
        rows.append(["% cung ước tính đang lỗ — thay đổi 7 ngày", _light(dl, good_below=-2, bad_above=2),
                     f"{mseries.loss_pct.iloc[-1]:.1f}% ({dl:+.1f} điểm %)", "Giảm: ít người lỗ hơn → áp lực bán hòa vốn nhẹ đi."])
    for col, name, note in (("tvl", "TVL hệ sinh thái — 30 ngày", "Tăng: vốn đang vào DeFi trên NEAR."),
                            ("stables", "Stablecoin trên NEAR — 30 ngày", "Tăng: thêm 'đạn' USD sẵn sàng trên chain.")):
        c = change_pct(df, col, 30)
        if c is not None:
            rows.append([name, _light(c, good_above=5, bad_below=-5), f"{c:+.1f}%", note])
    c = change_abs(df, "staking_ratio", 7)
    if c is not None:
        rows.append(["Tỷ lệ staking — 7 ngày", _light(c, good_above=0.2, bad_below=-0.2), f"{c:+.2f} điểm %",
                     "Tăng: thêm NEAR bị khóa stake → bớt cung lưu thông."])
    f, _ = last_val(df, "funding_apr")
    if f is not None:
        rows.append(["Funding perp (năm hóa)", "🔴" if f > 25 else ("🟢" if f < 0 else "🟡"), f"{f:+.1f}%/năm",
                     "Rất cao: long dùng đòn bẩy đông, dễ bị quét. Âm: short đông (thường là tín hiệu ngược)."])
    if whale_flow7 is not None:
        rows.append(["Ví cá voi theo dõi — dòng ròng 7 ngày", "🟢" if whale_flow7 > 0 else ("🔴" if whale_flow7 < 0 else "🟡"),
                     fmt_near(whale_flow7, signed=True), "Chỉ ví nhóm 'whale' trong near_watchlist.txt; tăng ≠ chắc chắn mua."])
    if near_btc_30 is not None:
        rows.append(["Sức mạnh so với BTC (NEAR/BTC 30 ngày)", _light(near_btc_30, good_above=3, bad_below=-3),
                     f"{near_btc_30:+.1f}%", "Dương: NEAR mạnh hơn BTC."])
    g = sum(r[1] == "🟢" for r in rows)
    r_ = sum(r[1] == "🔴" for r in rows)
    verdict = ("⚪ Chưa đủ dữ liệu" if not rows else "🟢 Nghiêng tích cực" if g - r_ >= 2
               else "🔴 Nghiêng tiêu cực" if r_ - g >= 2 else "🟡 Trung tính / trái chiều")
    return rows, g, r_, verdict


def daily_brief(live, df, mm, mseries, zones, whale_flow1):
    out = []
    p = live.get("price")
    if p:
        chg = f" ({live['change_24h']:+.2f}% 24h)" if live.get("change_24h") is not None else ""
        out.append(f"💲 Giá NEAR **{fmt_price(p)}**{chg}.")
    if mm.get("realized_price"):
        out.append(f"🧱 Giá vốn ước tính **{fmt_price(mm['realized_price'])}** — giá đang "
                   f"{(p / mm['realized_price'] - 1) * 100:+.1f}%; khoảng **{mm['loss_pct']:.0f}%** cung ước tính đang lỗ.")
    for col, name in (("tvl", "TVL"), ("stables", "Stablecoin")):
        v, _ = last_val(df, col)
        c7 = change_pct(df, col, 7)
        if v is not None:
            out.append(f"🏦 {name} trên NEAR **{fmt_big_usd(v)}**" + (f" ({c7:+.1f}% 7 ngày)." if c7 is not None else "."))
    sr, _ = last_val(df, "staking_ratio")
    if sr is not None:
        c = change_abs(df, "staking_ratio", 1)
        out.append(f"🔒 Đang stake **{sr:.1f}%** tổng cung" + (f" ({c:+.2f} điểm % so với hôm trước)." if c is not None else "."))
    f, _ = last_val(df, "funding_apr")
    if f is not None:
        out.append(f"📈 Funding perp **{f:+.1f}%/năm**.")
    if whale_flow1 is not None:
        out.append(f"🐋 Ví cá voi theo dõi: dòng ròng hôm nay **{fmt_near(whale_flow1, signed=True)}**.")
    sup, res = zones
    if sup or res:
        s = f"{fmt_price(sup[0]['low'])}–{fmt_price(sup[0]['high'])}" if sup else "N/A"
        r = f"{fmt_price(res[0]['low'])}–{fmt_price(res[0]['high'])}" if res else "N/A"
        out.append(f"🎯 Hỗ trợ gần nhất **{s}** · kháng cự gần nhất **{r}** (cụm giá vốn ước tính dày nhất).")
    return out


# =============================================================================
# CÁ VOI: danh sách ví & dòng tiền
# =============================================================================
def parse_watchlist(text):
    rows = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.split("|")]
        account = parts[0]
        if not re.match(r"^[a-z0-9._-]{2,64}$", account):
            continue
        label = parts[1] if len(parts) > 1 and parts[1] else account
        kind = parts[2].lower() if len(parts) > 2 else "unknown"
        rows.append({"account": account, "label": label, "type": kind if kind in VALID_TYPES else "unknown"})
    return rows


def whale_frame(data):
    rows = []
    for d, snap in (data.get("whales") or {}).items():
        if not is_day_key(d) or not isinstance(snap, dict):
            continue
        for acct, rec in snap.items():
            if isinstance(rec, dict) and safe_float(rec.get("balance")) is not None:
                rows.append({"date": parse_day(d), "account": acct, "label": rec.get("label", acct),
                             "type": rec.get("type", "unknown"), "balance": float(rec["balance"])})
    return pd.DataFrame(rows)


def group_flow(wf, kind, days):
    """Dòng ròng của nhóm ví: chỉ tính các ví có mặt ở cả hai ngày (tránh nhảy số khi thêm/bớt ví)."""
    if wf is None or wf.empty:
        return None
    g = wf[wf["type"] == kind]
    if g.empty:
        return None
    last = g["date"].max()
    old_day = g[g["date"] <= last - timedelta(days=days)]["date"].max()
    if pd.isna(old_day):
        return None
    a = g[g["date"] == last].set_index("account")["balance"]
    b = g[g["date"] == old_day].set_index("account")["balance"]
    common = a.index.intersection(b.index)
    return float((a[common] - b[common]).sum()) if len(common) else None


# =============================================================================
# CHẾ ĐỘ CẬP NHẬT — python pages/NEAR_Monitor.py --update-data
# =============================================================================
def _log(msg):
    print(f"[{utc_now():%H:%M:%S}] NEAR {redact(msg)}", flush=True)


def _merge(daily, rows, keep_keys=None):
    n = 0
    for d, rec in rows.items():
        if d is None or not isinstance(rec, dict):
            continue
        if d > utc_today():
            continue
        clean = {k: v for k, v in rec.items() if v is not None and (keep_keys is None or k in keep_keys)}
        if clean:
            daily.setdefault(day_str(d), {}).update(clean)
            n += 1
    return n


def run_near_update():
    try:
        with open(NEAR_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data = data if isinstance(data, dict) else {}
    except Exception:
        data = {}
    daily = data.setdefault("daily", {})
    whales = data.setdefault("whales", {})
    errors, done = {}, {}
    today = utc_today()

    # 1) Giá / vốn hóa / khối lượng — CoinGecko (lần đầu 365 ngày, sau đó 30 ngày gần nhất)
    have_price = sorted(k for k, v in daily.items() if isinstance(v, dict) and v.get("price"))
    days = 365 if len(have_price) < 300 else 30
    try:
        rows = fetch_coingecko_history(days)
        rows.pop(today, None)                     # điểm hôm nay chưa chốt ngày
        done["giá"] = _merge(daily, rows)
    except Exception as e:
        errors["coingecko"] = str(e)[:200]

    # 2) DeFiLlama: TVL, stablecoin, DEX
    rows, errs = fetch_defillama()
    done["defillama"] = _merge(daily, {d: r for d, r in rows.items() if d >= today - timedelta(days=HISTORY_DAYS)})
    if errs:
        errors["defillama"] = " | ".join(errs)[:300]

    # 3) Staking & tổng cung — RPC (ảnh chụp hôm nay)
    top_validators = []
    try:
        s = fetch_staking()
        top_validators = s.pop("top_validators", [])
        daily.setdefault(day_str(today), {}).update({k: v for k, v in s.items() if v is not None})
        done["staking"] = 1
    except Exception as e:
        errors["rpc"] = str(e)[:200]

    # 4) Phái sinh
    rows, now, errs = fetch_derivatives()
    done["phái sinh"] = _merge(daily, rows)
    if now:
        daily.setdefault(day_str(today), {}).update({k: v for k, v in now.items() if v is not None and k.startswith("hl_")})
        data["derivatives_meta"] = {k: v for k, v in now.items() if not k.startswith("hl_")}
    if errs:
        errors["phái sinh"] = " | ".join(errs)[:300]

    # 5) NearBlocks (tùy chọn)
    key = clean_secret(os.environ.get("NEARBLOCKS_API_KEY", ""))
    if key:
        rows, errs = fetch_nearblocks(key)
        done["nearblocks"] = _merge(daily, rows)
        if errs:
            errors["nearblocks"] = " | ".join(errs)[:300]

    # 6) Số dư ví theo dõi
    try:
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            watch = parse_watchlist(f.read())
    except FileNotFoundError:
        watch = []
    # Luôn theo dõi thêm 20 validator pool lớn nhất (dòng ủy thác stake vào/ra)
    listed = {w["account"] for w in watch}
    watch += [{"account": a, "label": f"Validator — {a}", "type": "validator"}
              for a in top_validators if a not in listed]
    snap, fails = {}, []
    for w in watch[:60]:
        try:
            b = fetch_account(w["account"])
            snap[w["account"]] = {**b, "label": w["label"], "type": w["type"]}
        except Exception as e:
            fails.append(f"{w['account']}: {str(e)[:80]}")
        time.sleep(0.15)
    if snap:
        whales[day_str(today)] = snap
        done["ví"] = len(snap)
    if fails:
        errors["ví"] = "; ".join(fails[:5])

    # Dọn & lưu
    data["daily"] = dict(sorted((k, v) for k, v in daily.items() if is_day_key(k))[-HISTORY_DAYS:])
    data["whales"] = dict(sorted((k, v) for k, v in whales.items() if is_day_key(k))[-400:])
    data["status"] = {"last_run_utc": utc_now().strftime("%Y-%m-%d %H:%M"), "updated": done,
                      "errors": {k: redact(v) for k, v in errors.items()}}
    with open(NEAR_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    _log(f"cập nhật {done}")
    for k, v in errors.items():
        _log(f"  ⚠️ {k}: {v}")


# =============================================================================
# LỚP DỮ LIỆU CHO DASHBOARD (chỉ đọc, có cache)
# =============================================================================
def _secret(name, default=""):
    try:
        v = st.secrets.get(name, None)
    except Exception:
        v = None
    return clean_secret(v) if v else clean_secret(os.environ.get(name, default)) or default


def read_data_file(filename):
    """Bản mới nhất trên GitHub (Actions vừa commit) → nếu lỗi thì file đi kèm bản deploy."""
    repo, branch = _secret("GITHUB_REPO"), _secret("GITHUB_BRANCH", "main") or "main"
    err = None
    if repo:
        try:
            r = requests.get(f"https://raw.githubusercontent.com/{repo}/{branch}/{filename}",
                             params={"t": int(time.time() // 300)}, headers=HTTP_HEADERS, timeout=30)
            if r.status_code == 200:
                data = json.loads(r.text)
                return (data if isinstance(data, dict) else {}), f"GitHub {repo}"
            err = f"GitHub HTTP {r.status_code}"
        except Exception as e:
            err = f"lỗi GitHub: {redact(e)[:120]}"
    try:
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
        return (data if isinstance(data, dict) else {}), "file trong bản deploy" + (f" ({err})" if err else "")
    except FileNotFoundError:
        return {}, "chưa có dữ liệu" + (f" ({err})" if err else "")
    except Exception as e:
        return {}, f"lỗi đọc file: {e}"


@st.cache_data(ttl=900, show_spinner="Đang tải dữ liệu NEAR…")
def load_near():
    data, source = read_data_file(NEAR_FILE)
    return data, source


@st.cache_data(ttl=900, show_spinner=False)
def load_btc_prices():
    data, _ = read_data_file(BTC_PRICE_FILE)
    rows = [(parse_day(d), safe_float(r.get("btc_price"))) for d, r in (data.get("daily") or {}).items()
            if is_day_key(d) and isinstance(r, dict)]
    return pd.DataFrame([(d, p) for d, p in rows if p], columns=["date", "btc_price"]).sort_values("date")


@st.cache_data(ttl=300, show_spinner=False)
def live_near():
    for fn in (fetch_coingecko_now, fetch_coinbase_now):
        try:
            d = fn()
            if d.get("price"):
                return d
        except Exception:
            continue
    return {}


@st.cache_data(ttl=900, show_spinner=False)
def candles_cached(days):
    try:
        df, src = fetch_candles(days)
        return df, src, None
    except Exception as e:
        return None, None, str(e)


@st.cache_data(ttl=900, show_spinner=False)
def model_cached(version, alpha):
    data, _ = load_near()
    return cost_basis_model(daily_frame(data), alpha=alpha)


# =============================================================================
# GIAO DIỆN
# =============================================================================
def _st_version():
    try:
        return tuple(int(x) for x in re.findall(r"\d+", st.__version__)[:2])
    except Exception:
        return (0, 0)


STRETCH = {"width": "stretch"} if _st_version() >= (1, 50) else {"use_container_width": True}
BG = "#0b0f14"
GREEN, RED, AMBER, CYAN, GREY, NEAR_GREEN = "#10b981", "#ef4444", "#f59e0b", "#22d3ee", "#94a3b8", "#00ec97"


def _esc(text):
    return re.sub(r"(?<!\\)\$", r"\\$", str(text))


def mk(text, **kw):
    st.markdown(_esc(text), **kw)


def cap(text, **kw):
    st.caption(_esc(text), **kw)


def style_fig(fig, height=440, hovermode="x unified"):
    fig.update_layout(template="plotly_dark", height=height, paper_bgcolor=BG, plot_bgcolor=BG,
                      margin=dict(l=55, r=40, t=45, b=40), hovermode=hovermode,
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, bgcolor="rgba(0,0,0,0)"))
    return fig


def show_fig(fig, key):
    st.plotly_chart(fig, key=key, config={"displaylogo": False}, theme=None, **STRETCH)


def show_df(df):
    st.dataframe(df, hide_index=True, **STRETCH)


def inject_css():
    st.markdown("""
<style>
.block-container {padding-top: 2rem; max-width: 1500px;}
[data-testid="stMetric"] {background: linear-gradient(135deg, rgba(20,40,38,.95), rgba(15,23,42,.95));
  border: 1px solid rgba(0,236,151,.22); padding: 14px 18px; border-radius: 14px;}
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] * {font-size: .88rem; color: #cbd5e1 !important;}
[data-testid="stMetricValue"] {font-size: 1.45rem; color: #f8fafc !important;}
div[data-testid="stDataFrame"] {border-radius: 12px; overflow: hidden;}
</style>""", unsafe_allow_html=True)


def _with_price_axis(fig, prices, secondary=True):
    if prices is not None and not prices.empty:
        fig.add_scatter(x=prices.date, y=prices.price, name="Giá NEAR", mode="lines",
                        line=dict(color="#f8fafc", width=1.6), secondary_y=secondary)


def zones_table(zones):
    return pd.DataFrame([{"Vùng giá": f"{fmt_price(z['low'])} – {fmt_price(z['high'])}",
                          "NEAR giá vốn (ước tính)": fmt_near(z["near"]),
                          "% phần đã ước tính": f"{z['share_pct']:.1f}%",
                          "Cách giá": f"{z['distance_pct']:+.1f}%"} for z in zones])


# ----------------------------------------------------------------------------- Tổng quan
def tab_overview(live, df, mm, mseries, zones, wf, near_btc_30, events_note):
    price = live.get("price")
    w1, w7 = group_flow(wf, "whale", 1), group_flow(wf, "whale", 7)

    mk("### 📰 Hôm nay có gì thay đổi")
    brief = daily_brief(live, df, mm, mseries, zones, w1)
    mk("\n".join(f"- {b}" for b in brief) if brief else "Chưa đủ dữ liệu.")

    c = st.columns(4)
    c[0].metric("Giá NEAR", fmt_price(price), f"{live['change_24h']:+.2f}% 24h" if live.get("change_24h") is not None else None)
    mcap, _ = last_val(df, "market_cap")
    c[1].metric("Vốn hóa", fmt_big_usd(live.get("market_cap") or mcap),
                f"FDV {fmt_big_usd(live['fdv'])}" if live.get("fdv") else None, delta_color="off")
    c[2].metric("Giá vốn ước tính", fmt_price(mm.get("realized_price")),
                f"giá {(price / mm['realized_price'] - 1) * 100:+.1f}%" if mm.get("realized_price") and price else None,
                help="Mô hình từ giá + khối lượng 1–2 năm (xem tab Giá vốn ước tính).")
    c[3].metric("Cung ước tính đang lỗ", f"{mm['loss_pct']:.1f}%" if mm.get("loss_pct") is not None else "N/A",
                (f"{mseries.loss_pct.iloc[-1] - mseries.loss_pct.iloc[-2]:+.1f} điểm % (1D)"
                 if mseries is not None and len(mseries) > 1 else None), delta_color="inverse")

    c = st.columns(4)
    for i, (col, name) in enumerate((("tvl", "TVL hệ sinh thái"), ("stables", "Stablecoin trên NEAR"))):
        v, _ = last_val(df, col)
        ch = change_pct(df, col, 30)
        c[i].metric(name, fmt_big_usd(v), f"{ch:+.1f}% 30D" if ch is not None else None)
    sr, _ = last_val(df, "staking_ratio")
    ch = change_abs(df, "staking_ratio", 7)
    c[2].metric("Tỷ lệ staking", f"{sr:.1f}%" if sr is not None else "N/A",
                f"{ch:+.2f} điểm % 7D" if ch is not None else None)
    f, _ = last_val(df, "funding_apr")
    c[3].metric("Funding perp (năm hóa)", f"{f:+.1f}%" if f is not None else "N/A",
                "OKX" if f is not None else None, delta_color="off")

    mk("### 🎯 Vùng hỗ trợ / kháng cự (cụm giá vốn ước tính)")
    sup, res = zones
    a, b = st.columns(2)
    with a:
        mk("**🟢 Hỗ trợ (dưới giá)**")
        if sup:
            show_df(zones_table(sup))
        else:
            cap("Chưa có dữ liệu.")
    with b:
        mk("**🔴 Kháng cự (trên giá)**")
        if res:
            show_df(zones_table(res))
        else:
            cap("Chưa có dữ liệu.")
    if sup:
        mk(f"- Mất vùng **{fmt_price(sup[0]['low'])}–{fmt_price(sup[0]['high'])}** thì vùng đỡ kế tiếp là "
           + (f"{fmt_price(sup[1]['low'])}–{fmt_price(sup[1]['high'])}." if len(sup) > 1 else "chưa xác định."))
    if res:
        mk(f"- Vượt và giữ trên **{fmt_price(res[0]['low'])}–{fmt_price(res[0]['high'])}** thì vùng cản kế tiếp là "
           + (f"{fmt_price(res[1]['low'])}–{fmt_price(res[1]['high'])}." if len(res) > 1 else "chưa xác định."))

    mk("### 🚦 Tín hiệu tổng hợp")
    rows, g, r, verdict = build_signals(price, df, mm, mseries, w7, near_btc_30)
    mk(f"#### {verdict}")
    cap(f"{g} 🟢 · {len(rows) - g - r} 🟡 · {r} 🔴 trên {len(rows)} tín hiệu. "
        "Chỉ báo đếm tín hiệu — không phải xác suất, dự báo hay khuyến nghị mua/bán.")
    if rows:
        show_df(pd.DataFrame(rows, columns=["Tín hiệu", "Đèn", "Giá trị", "Cách đọc"]))
    if events_note:
        cap(events_note)


# ----------------------------------------------------------------------------- Giá vốn ước tính
def tab_cost_basis(version, price, df):
    alpha = st.select_slider("Hệ số luân chuyển (mức khối lượng sàn được coi là đổi chủ thật)",
                             options=[0.05, 0.1, 0.15, 0.2, 0.3, 0.5], value=0.2, key="near_alpha",
                             help="Thấp = coi phần lớn khối lượng là giao dịch qua lại, giá vốn thay đổi chậm. "
                                  "Cao = giá vốn bám sát giá gần đây.")
    model = model_cached(version, alpha)
    if not model:
        st.info("Cần ít nhất 30 ngày giá + khối lượng. Chạy workflow **Update BTC data** để tải lịch sử NEAR.")
        return
    mm = model_metrics(model, price)
    sup, res = find_zones(model, price)
    m = st.columns(4)
    m[0].metric("Giá vốn ước tính", fmt_price(mm["realized_price"]))
    m[1].metric("Giá / giá vốn", f"{mm['mvrv']:.2f}x")
    m[2].metric("Cung ước tính đang lỗ", f"{mm['loss_pct']:.1f}%")
    m[3].metric("Phần cung đã ước tính được", f"{mm['known_pct']:.0f}%",
                help="Phần còn lại chưa đổi chủ trong cửa sổ dữ liệu → giá vốn cũ hơn, không xác định.")

    mids = (model["edges"][:-1] + model["edges"][1:]) / 2
    fig = go.Figure()
    fig.add_bar(x=mids, y=model["dist"], width=model["step"] * 0.92, marker_color=np.where(mids < price, GREEN, RED),
                hovertemplate="$%{x:.3f}<br>%{y:,.0f} NEAR<extra></extra>", name="Giá vốn ước tính")
    for z in sup:
        fig.add_vrect(x0=z["low"], x1=z["high"], fillcolor=GREEN, opacity=0.10, line_width=0, layer="below")
    for z in res:
        fig.add_vrect(x0=z["low"], x1=z["high"], fillcolor=RED, opacity=0.10, line_width=0, layer="below")
    fig.add_vline(x=price, line_dash="dash", line_color="white", annotation_text=f"Giá {fmt_price(price)}",
                  annotation_position="top left")
    fig.add_vline(x=mm["realized_price"], line_dash="dot", line_color=AMBER,
                  annotation_text=f"Giá vốn TB {fmt_price(mm['realized_price'])}", annotation_position="top right",
                  annotation_font_color=AMBER)
    style_fig(fig, 500, hovermode="closest")
    nz = np.where(model["dist"] > model["dist"].max() * 0.002)[0]
    x0 = float(model["edges"][nz.min()]) if nz.size else 0.0
    x1 = float(model["edges"][nz.max() + 1]) if nz.size else float(model["edges"][-1])
    x0, x1 = min(x0, price) * 0.92, max(x1, price) * 1.05
    fig.update_layout(showlegend=False, bargap=0.02,
                      xaxis=dict(title="Giá vốn ước tính (USD)", tickprefix="$", range=[x0, x1]),
                      yaxis=dict(title="NEAR"))
    show_fig(fig, "near_cb")
    cap("🟢 giá vốn dưới giá hiện tại (đang lãi) · 🔴 trên giá (đang lỗ) · nền xanh/đỏ = vùng hỗ trợ/kháng cự.")

    mk("#### 🌡️ Giá vốn ước tính theo thời gian")
    z = model["snaps"]
    positive = z[z > 0]
    zmax = float(np.percentile(positive, 99)) if positive.size else 1.0
    heat = go.Figure(go.Heatmap(x=model["dates"], y=mids, z=np.clip(z, 0, zmax), zmin=0, zmax=zmax,
                                colorscale=[[0, "#0b1d1a"], [0.2, "#123d36"], [0.45, "#0f766e"], [0.7, "#00c08b"],
                                            [1, "#d9ff7a"]], colorbar=dict(title="NEAR"),
                                hovertemplate="%{x}<br>$%{y:.3f}<br>%{z:,.0f} NEAR<extra></extra>"))
    heat.add_scatter(x=model["dates"], y=model["prices"], mode="lines", name="Giá NEAR",
                     line=dict(color="#ffffff", width=1.8))
    style_fig(heat, 520, hovermode="closest")
    heat.update_yaxes(range=[float(np.min(model["prices"])) * 0.8, float(np.max(model["prices"])) * 1.1], tickprefix="$")
    show_fig(heat, "near_cb_heat")

    with st.expander("🧠 Mô hình giá vốn ước tính hoạt động thế nào?"):
        mk("- NEAR không có UTXO nên **không đo được** giá vốn on-chain như URPD của BTC. Đây là **mô hình ước tính** "
           "từ giá và tổng khối lượng giao dịch (CoinGecko) theo ngày.\n"
           "- Mỗi ngày, một phần nguồn cung = hệ số luân chuyển × khối lượng / cung lưu hành được coi là đã đổi chủ, "
           "và giá vốn của phần đó chuyển về giá ngày hôm đó. NEAR mới phát hành được ghi giá vốn tại giá ngày phát hành.\n"
           "- Khối lượng sàn gồm nhiều giao dịch qua lại, nên hệ số mặc định 0.2. Đổi hệ số để xem độ nhạy: "
           "vùng nào vẫn dày ở mọi hệ số là vùng đáng tin hơn.\n"
           "- Dùng để tham khảo vùng giá có nhiều người mua — không phải dữ liệu ví thật.")

    mk("#### 📊 Volume profile ngắn hạn (khối lượng khớp theo vùng giá)")
    days = st.radio("Khoảng", [30, 60], index=1, horizontal=True, format_func=lambda x: f"{x} ngày", key="near_vp_days")
    candles, src, err = candles_cached(days)
    if candles is None or candles.empty:
        st.info(f"Chưa lấy được nến giao dịch: {err}")
        return
    lo, hi = float(candles.low.min()), float(candles.high.max())
    edges = np.linspace(lo, hi, 61)
    prof = np.zeros(60)
    for l, h, v in zip(candles.low, candles.high, candles.quote_volume):
        if h <= l:
            prof[min(59, int(np.searchsorted(edges, l) - 1))] += v
            continue
        ov = np.clip(np.minimum(edges[1:], h) - np.maximum(edges[:-1], l), 0, None)
        if ov.sum() > 0:
            prof += v * ov / ov.sum()
    centers = (edges[:-1] + edges[1:]) / 2
    vp = go.Figure(go.Bar(x=prof, y=centers, orientation="h",
                          marker_color=np.where(centers < price, GREEN, RED),
                          hovertemplate="$%{y:.3f}<br>$%{x:,.0f}<extra></extra>"))
    vp.add_hline(y=price, line_dash="dash", line_color="white", annotation_text=f"Giá {fmt_price(price)}")
    style_fig(vp, 460, hovermode="closest")
    vp.update_layout(showlegend=False, xaxis_title="Khối lượng (USD)", yaxis=dict(tickprefix="$"))
    show_fig(vp, "near_vp")
    cap(f"Nguồn: {src}. Khối lượng của mỗi nến được chia đều theo biên độ giá của nến đó.")


# ----------------------------------------------------------------------------- Hệ sinh thái & staking
def tab_ecosystem(df, prices, days):
    d = df[df["date"] >= df["date"].max() - timedelta(days=days - 1)] if not df.empty else df
    if "tvl" in d.columns or "stables" in d.columns:
        mk("#### 🏦 TVL & stablecoin trên NEAR")
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        for col, name, color in (("tvl", "TVL", NEAR_GREEN), ("stables", "Stablecoin", CYAN)):
            if col in d.columns and d[col].notna().any():
                fig.add_scatter(x=d.date, y=d[col], name=name, line=dict(color=color, width=2), secondary_y=False)
        _with_price_axis(fig, prices)
        style_fig(fig, 420)
        fig.update_yaxes(title_text="USD", secondary_y=False)
        fig.update_yaxes(title_text="Giá NEAR", tickprefix="$", showgrid=False, secondary_y=True)
        show_fig(fig, "near_tvl")
    if "dex_volume" in d.columns and d["dex_volume"].notna().any():
        mk("#### 🔁 Khối lượng DEX trên NEAR (USD/ngày)")
        dv = d.dropna(subset=["dex_volume"])
        fig = go.Figure(go.Bar(x=dv.date, y=dv.dex_volume, marker_color=NEAR_GREEN, name="DEX volume"))
        fig.add_scatter(x=dv.date, y=dv.dex_volume.rolling(7, min_periods=1).mean(), name="TB 7 ngày",
                        line=dict(color=AMBER, width=2))
        style_fig(fig, 340)
        show_fig(fig, "near_dex")

    mk("#### 🔒 Staking & lạm phát")
    m = st.columns(4)
    for i, (col, name, fmt) in enumerate((("total_stake", "Tổng NEAR đang stake", fmt_near),
                                          ("staking_ratio", "Tỷ lệ staking", lambda v: f"{v:.1f}%"),
                                          ("validators", "Số validator", lambda v: f"{v:,.0f}"),
                                          ("top10_stake_share", "Top 10 validator nắm", lambda v: f"{v:.1f}% stake"))):
        v, _ = last_val(df, col)
        m[i].metric(name, fmt(v) if v is not None else "N/A")
    ts = df.dropna(subset=["total_supply"]) if "total_supply" in df.columns else pd.DataFrame()
    if len(ts) >= 8:
        a, b = ts.iloc[-1], ts[ts.date <= ts.date.iloc[-1] - timedelta(days=7)]
        if not b.empty:
            b = b.iloc[-1]
            yrs = (a.date - b.date).days / 365
            infl = ((a.total_supply / b.total_supply) ** (1 / yrs) - 1) * 100 if yrs > 0 else None
            cap(f"Lạm phát đo được (tổng cung tăng, năm hóa): **{infl:.2f}%/năm** — từ {b.date:%d/%m} đến {a.date:%d/%m}. "
                "Từ bản nâng cấp 2.9.0 (cuối 2025), lạm phát tối đa của NEAR giảm từ 5% xuống 2.5%/năm.")
    if "staking_ratio" in df.columns and df["staking_ratio"].notna().sum() >= 2:
        sr = df.dropna(subset=["staking_ratio"])
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_scatter(x=sr.date, y=sr.staking_ratio, name="Tỷ lệ staking (%)", line=dict(color=NEAR_GREEN, width=2),
                        secondary_y=False)
        _with_price_axis(fig, prices)
        style_fig(fig, 340)
        show_fig(fig, "near_stake")
    else:
        cap("Lịch sử staking được ghi từ ngày bắt đầu chạy GitHub Actions và dày dần theo thời gian.")

    if "txns" in df.columns and df["txns"].notna().any():
        mk("#### 🌐 Hoạt động mạng (NearBlocks)")
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_bar(x=d.date, y=d.get("txns"), name="Giao dịch/ngày", marker_color="rgba(0,236,151,.6)", secondary_y=False)
        if "active_accounts" in d.columns:
            fig.add_scatter(x=d.date, y=d.active_accounts, name="Tài khoản hoạt động", line=dict(color=AMBER),
                            secondary_y=True)
        style_fig(fig, 340)
        show_fig(fig, "near_activity")
    else:
        cap("Hoạt động mạng (số giao dịch, tài khoản hoạt động) cần NEARBLOCKS_API_KEY trong secrets của GitHub Actions.")


# ----------------------------------------------------------------------------- Phái sinh
def tab_derivatives(df, prices, days, meta):
    d = df[df["date"] >= df["date"].max() - timedelta(days=days - 1)] if not df.empty else df
    cols = [c for c in ("funding_apr", "oi_usd", "long_short_ratio") if c in d.columns and d[c].notna().any()]
    if not cols:
        st.info("Chưa có dữ liệu phái sinh (OKX/Hyperliquid).")
        return
    m = st.columns(4)
    f, _ = last_val(df, "funding_apr")
    oi, _ = last_val(df, "oi_usd")
    ls, _ = last_val(df, "long_short_ratio")
    hl, _ = last_val(df, "hl_funding_apr")
    m[0].metric("Funding OKX (năm hóa)", f"{f:+.1f}%" if f is not None else "N/A")
    m[1].metric("Open interest OKX", fmt_big_usd(oi), f"{change_pct(df, 'oi_usd', 7):+.1f}% 7D"
                if change_pct(df, "oi_usd", 7) is not None else None)
    m[2].metric("Tỷ lệ tài khoản long/short", f"{ls:.2f}" if ls is not None else "N/A")
    m[3].metric("Funding Hyperliquid (năm hóa)", f"{hl:+.1f}%" if hl is not None else "N/A")
    fig = make_subplots(rows=len(cols), cols=1, shared_xaxes=True, vertical_spacing=0.07,
                        subplot_titles=[{"funding_apr": "Funding năm hóa (%)", "oi_usd": "Open interest (USD)",
                                         "long_short_ratio": "Tỷ lệ tài khoản long/short"}[c] for c in cols])
    for i, c in enumerate(cols, start=1):
        s = d.dropna(subset=[c])
        if c == "funding_apr":
            fig.add_bar(x=s.date, y=s[c], marker_color=np.where(s[c] >= 0, GREEN, RED), name=c, row=i, col=1)
        else:
            fig.add_scatter(x=s.date, y=s[c], line=dict(color=CYAN if c == "oi_usd" else AMBER, width=2), name=c,
                            row=i, col=1)
    style_fig(fig, 240 * len(cols) + 60)
    fig.update_layout(showlegend=False)
    show_fig(fig, "near_deriv")
    interval = (meta or {}).get("funding_interval_h")
    cap("Nguồn: OKX (NEAR-USDT-SWAP)" + (f", funding mỗi {interval:.0f} giờ" if interval else "") +
        "; Hyperliquid làm dự phòng. Funding dương cao + OI tăng nhanh = đòn bẩy long dồn, dễ có cú quét; "
        "funding âm kéo dài = short đông.")


# ----------------------------------------------------------------------------- Cá voi
TYPE_VI = {"whale": "🐋 Cá voi", "exchange": "🏦 Ví sàn", "validator": "🛡️ Validator", "protocol": "⚙️ Giao thức",
           "unknown": "❔ Khác"}


def tab_whales(wf, repo):
    link = f"https://github.com/{repo}/edit/main/{WATCHLIST_FILE}" if repo else None
    cap("Số dư native NEAR (liquid + locked) của các ví trong near_watchlist.txt, GitHub Actions chụp mỗi ngày. "
        "Số dư tăng/giảm không khẳng định mua/bán; ví sàn phải tự xác minh trước khi gắn nhãn 'exchange'.")
    if link:
        mk(f"✏️ Sửa danh sách ví: [{WATCHLIST_FILE}]({link}) — mỗi dòng `account.near | Tên | loại` "
           "(loại: whale, exchange, validator, protocol, unknown).")
    if wf is None or wf.empty:
        st.info("Chưa có ảnh chụp số dư nào. Chạy workflow để tạo ngày đầu tiên.")
        return
    types = [t for t in ("whale", "exchange", "validator", "protocol", "unknown") if t in set(wf["type"])]
    last = wf["date"].max()
    m = st.columns(len(types))
    for i, t in enumerate(types):
        total = float(wf[(wf.type == t) & (wf.date == last)].balance.sum())
        f7 = group_flow(wf, t, 7)
        m[i].metric(f"{TYPE_VI[t]} ({wf[(wf.type == t) & (wf.date == last)].account.nunique()} ví)", fmt_near(total),
                    f"{fmt_near(f7, signed=True)} 7D" if f7 is not None else None)
    rows = []
    for t in types:
        rows.append({"Nhóm": TYPE_VI[t], **{f"Dòng ròng {n}D": fmt_near(group_flow(wf, t, n), signed=True)
                                    if group_flow(wf, t, n) is not None else "—" for n in (1, 7, 30)}})
    show_df(pd.DataFrame(rows))
    if wf["date"].nunique() >= 2:
        mk("#### Dòng ròng tích lũy theo nhóm (so với ngày đầu có dữ liệu)")
        fig = go.Figure()
        for t in types:
            g = wf[wf.type == t]
            first_day = g.date.min()
            base = g[g.date == first_day].set_index("account").balance
            g = g[g.account.isin(base.index)]          # chỉ ví có từ đầu → không nhảy số khi thêm ví mới
            s = g.groupby("date").balance.sum() - base.sum()
            fig.add_scatter(x=s.index, y=s.values, name=TYPE_VI[t], mode="lines+markers")
        fig.add_hline(y=0, line_color=GREY, line_dash="dot")
        style_fig(fig, 360)
        fig.update_yaxes(title_text="NEAR")
        show_fig(fig, "near_whale_total")
    cur = wf[wf.date == last].copy()
    for n in (1, 7, 30):
        old_day = wf[wf.date <= last - timedelta(days=n)].date.max()
        old = wf[wf.date == old_day].set_index("account").balance if not pd.isna(old_day) else pd.Series(dtype=float)
        cur[f"Δ {n}D"] = cur.apply(lambda r: fmt_near(r.balance - old[r.account], signed=True)
                                   if r.account in old.index else "—", axis=1)
    cur = cur.sort_values("balance", ascending=False)
    show_df(pd.DataFrame({"Ví": cur.label, "Account": cur.account, "Loại": cur.type.map(TYPE_VI),
                          "Số dư": cur.balance.map(fmt_near), "Δ 1D": cur["Δ 1D"], "Δ 7D": cur["Δ 7D"],
                          "Δ 30D": cur["Δ 30D"]}))


# ----------------------------------------------------------------------------- Giá & xu hướng
def tab_trend(df, btc, days, mseries):
    p = df.dropna(subset=["price"]) if "price" in df.columns else pd.DataFrame()
    if p.empty:
        st.info("Chưa có lịch sử giá.")
        return
    p = p.copy()
    for span in (20, 50, 200):
        p[f"EMA{span}"] = ema(p.price, span)
    v = p[p.date >= p.date.max() - timedelta(days=days - 1)]
    fig = go.Figure()
    fig.add_scatter(x=v.date, y=v.price, name="Giá NEAR", line=dict(color="#f8fafc", width=2))
    for span, color in ((20, NEAR_GREEN), (50, AMBER), (200, RED)):
        fig.add_scatter(x=v.date, y=v[f"EMA{span}"], name=f"EMA{span}", line=dict(color=color, width=1.4))
    if mseries is not None and not mseries.empty:
        ms = mseries[mseries.date >= v.date.min()]
        fig.add_scatter(x=ms.date, y=ms.realized_price, name="Giá vốn ước tính", line=dict(color=CYAN, dash="dot"))
    style_fig(fig, 460)
    fig.update_yaxes(tickprefix="$")
    show_fig(fig, "near_trend")
    m = st.columns(3)
    for i, span in enumerate((20, 50, 200)):
        val = float(p[f"EMA{span}"].iloc[-1])
        m[i].metric(f"EMA{span}", fmt_price(val), f"giá {(p.price.iloc[-1] / val - 1) * 100:+.1f}%")
    if btc is not None and not btc.empty:
        r = p.merge(btc, on="date", how="inner")
        r = r[r.date >= r.date.max() - timedelta(days=days - 1)] if not r.empty else r
        if len(r) > 5:
            r["ratio"] = r.price / r.btc_price * 1e5
            mk("#### ⚖️ NEAR/BTC (× 100.000)")
            fig = go.Figure(go.Scatter(x=r.date, y=r.ratio, line=dict(color=NEAR_GREEN, width=2), name="NEAR/BTC"))
            style_fig(fig, 300)
            show_fig(fig, "near_btc")
            cap("Đường đi lên = NEAR mạnh hơn BTC. Giá BTC lấy từ dữ liệu của trang BTC.")


def near_btc_change(df, btc, days=30):
    if btc is None or btc.empty or "price" not in df.columns:
        return None
    r = df.dropna(subset=["price"]).merge(btc, on="date", how="inner")
    if len(r) < days // 2:
        return None
    r["ratio"] = r.price / r.btc_price
    last = r.iloc[-1]
    old = r[r.date <= last.date - timedelta(days=days)]
    return (last.ratio / old.ratio.iloc[-1] - 1) * 100 if not old.empty else None


def main_near():
    st.set_page_config(page_title="NEAR Monitor", page_icon="🟢", layout="wide")
    inject_css()
    data, source = load_near()
    df = daily_frame(data)
    status = data.get("status", {}) if isinstance(data, dict) else {}
    live = live_near()
    if not live.get("price") and "price" in df.columns and df.price.notna().any():
        live = {"price": float(df.price.dropna().iloc[-1]), "source": "giá đóng cửa gần nhất"}

    head, btn = st.columns([5, 1])
    with head:
        st.title("🟢 NEAR — bảng theo dõi hằng ngày")
    with btn:
        st.write("")
        if st.button("🔄 Tải lại", **STRETCH):
            st.cache_data.clear()
            st.rerun()
    last_run = status.get("last_run_utc")
    when = (datetime.strptime(last_run, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).astimezone(VN_TZ)
            .strftime("%d/%m %H:%M") if last_run else "chưa chạy")
    cap(f"Dữ liệu: {source} • GitHub Actions cập nhật lần cuối {when} giờ VN • giá trực tiếp {live.get('source', 'N/A')} • "
        f"NEAR Monitor {NEAR_VERSION}")
    for k, v in (status.get("errors") or {}).items():
        cap(f"⚠️ {k}: {v[:160]}")
    if df.empty or not live.get("price"):
        st.warning("Chưa có dữ liệu NEAR. Thêm bước NEAR vào workflow rồi bấm **Run workflow** trong tab Actions của GitHub.")
        return
    if last_run and (utc_now() - datetime.strptime(last_run, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)).days >= 2:
        st.warning("⚠️ Dữ liệu NEAR đã cũ hơn 2 ngày — kiểm tra GitHub Actions.")

    price = live["price"]
    version = f"{last_run}|{len(df)}"
    model = model_cached(version, 0.2)
    mm = model_metrics(model, price)
    mseries = model_series(model)
    zones = find_zones(model, price)
    wf = whale_frame(data)
    btc = load_btc_prices()
    nb30 = near_btc_change(df, btc)
    prices = df.dropna(subset=["price"])[["date", "price"]] if "price" in df.columns else pd.DataFrame()

    tabs = st.tabs(["📊 Tổng quan", "🧱 Giá vốn ước tính", "🏦 Hệ sinh thái & Staking", "📈 Phái sinh",
                    "🐋 Cá voi", "📉 Giá & xu hướng"])
    with tabs[0]:
        tab_overview(live, df, mm, mseries, zones, wf, nb30, None)
    with tabs[1]:
        tab_cost_basis(version, price, df)
    with tabs[2]:
        days = st.radio("Khoảng thời gian", [90, 180, 365], index=1, horizontal=True,
                        format_func=lambda x: f"{x} ngày", key="near_eco_days")
        tab_ecosystem(df, prices[prices.date >= prices.date.max() - timedelta(days=days - 1)] if not prices.empty else prices,
                      days)
    with tabs[3]:
        days = st.radio("Khoảng thời gian", [30, 90, 180], index=1, horizontal=True,
                        format_func=lambda x: f"{x} ngày", key="near_der_days")
        tab_derivatives(df, prices, days, data.get("derivatives_meta"))
    with tabs[4]:
        tab_whales(wf, _secret("GITHUB_REPO"))
    with tabs[5]:
        days = st.radio("Khoảng thời gian", [90, 180, 365], index=1, horizontal=True,
                        format_func=lambda x: f"{x} ngày", key="near_trend_days")
        tab_trend(df, btc, days, mseries)


if UPDATE_MODE:
    run_near_update()
else:
    main_near()
