"""
LINK Monitor V1 — trang riêng (pages/LINK_Monitor.py) cùng kiến trúc với BTC URPD Monitor / NEAR Monitor.

Hai chế độ chạy:
  • Dashboard:          Streamlit tự chạy file này khi mở trang "LINK Monitor". CHỈ ĐỌC link_history.json.
  • Cập nhật dữ liệu:   python pages/LINK_Monitor.py --update-data   (GitHub Actions gọi cùng lần chạy BTC/NEAR)
                        Giá/khối lượng (CoinGecko), phái sinh (OKX + Hyperliquid), số dư LINK trên các ví sàn
                        & pool staking Chainlink (Blockscout — miễn phí, không cần key), tin tức (Google News).
                        ETF LINK (GLNK, CLNK…) đọc từ etf_flows_history.json do app BTC lấy sẵn.

Không theo dõi từng ví cá mập — chỉ tổng số dư các ví sàn có gắn nhãn (Binance, Coinbase, Kraken, OKX, …)
trong ~300 ví giữ nhiều LINK nhất → biết LINK đang được NẠP lên sàn (áp lực bán) hay RÚT về (gom).
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

LINK_VERSION = "V1.0"
UPDATE_MODE = "--update-data" in sys.argv

LINK_FILE = "link_history.json"
ETF_FILE = "etf_flows_history.json"          # app BTC lấy dòng tiền ETF (gồm ETF LINK)
REPORT_FILE = "daily_report.json"            # dùng chung với BTC/NEAR để báo cáo nhanh
LINK_CG_ID = "chainlink"
LINK_TOKEN = "0x514910771AF9Ca656af840dff83E8264EcF986CA"
BLOCKSCOUT = "https://eth.blockscout.com"
HOLDER_PAGES = 6                              # 6 trang × 50 = 300 ví giữ nhiều LINK nhất (~ từ 300k LINK)
STAKING_POOLS = {                             # Chainlink Staking v0.2 (Ethereum)
    "community": "0xBc10f2E862ED4502144c7d632a3459F49DFCDB5e",
    "operator": "0xA1d76A7cA72128541E9FCAcafBdA3a92EF94fDc5",
}
REWARD_VAULT = "0x996913c8c08472f584ab8834e925b06D0eb1D813"
EXCHANGE_BRANDS = ["Binance", "Coinbase", "Kraken", "OKX", "Bybit", "Bitget", "Upbit", "Bithumb", "Gemini",
                   "Crypto.com", "Robinhood", "Bitpanda", "KuCoin", "Gate.io", "HTX", "Huobi", "Bitfinex", "MEXC",
                   "eToro", "Paxos", "CoinSpot", "Bitstamp", "Korbit", "Coinone", "BingX", "Poloniex", "Bitvavo"]
EXCHANGE_RE = re.compile(r"exchange|hot wallet|cold wallet|deposit address|" +
                         "|".join(re.escape(b) for b in EXCHANGE_BRANDS), re.I)
NOT_EXCHANGE_RE = re.compile(r"layer 2|bridge|gateway|staking|etp|etf|aave|compound|uniswap|reserve", re.I)
HISTORY_DAYS = 730
VN_TZ = timezone(timedelta(hours=7))
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 LINK-Monitor/1", "Accept": "application/json"}
LINK_NEWS_QUERIES = ['"Chainlink" LINK', '"Chainlink" CCIP OR "Chainlink Reserve" OR "Sergey Nazarov"',
                     '"LINK price" OR "Chainlink price" OR "Chainlink ETF"']
REPORT_WEIGHTS = {"Giá & xu hướng": 0.30, "Phái sinh": 0.20, "Dòng tiền (ETF · sàn · staking)": 0.30,
                  "Khối lượng": 0.10, "Tin tức": 0.10}
BG = "#0b0f14"
GREEN, RED, AMBER, CYAN, GREY, LINK_BLUE = "#10b981", "#ef4444", "#f59e0b", "#22d3ee", "#94a3b8", "#375bd2"


def redact(msg):
    text = str(msg)
    for name in ("COINGECKO_API_KEY", "GITHUB_TOKEN"):
        v = clean_secret(os.environ.get(name, ""))
        if v and len(v) >= 6:
            text = text.replace(v, "***")
    return re.sub(r"github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9]{20,}", "***", text)


def fmt_link(x, signed=False):
    if x is None or not np.isfinite(x):
        return "N/A"
    a = abs(x)
    sign = ("+" if x >= 0 else "-") if signed else ("-" if x < 0 else "")
    if a >= 1e6:
        return f"{sign}{a / 1e6:.2f}tr LINK"
    return f"{sign}{a:,.0f} LINK"


def _log(msg):
    print(f"[{utc_now().strftime('%H:%M:%S')}] LINK {msg}", flush=True)


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
    sign = ("+" if x >= 0 else "-") if signed else ("-" if x < 0 else "")
    a = abs(x)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{sign}${a / div:.2f}{suf}"
    return f"{sign}${a:,.0f}"


def clean_secret(raw):
    """Lấy đúng giá trị kể cả khi lỡ dán 'TÊN = "giá trị"' hoặc nhiều dòng."""
    for line in str(raw or "").strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^[A-Za-z_][A-Za-z0-9_]*\s*=\s*(.+)$', line)
        return (m.group(1) if m else line).strip().strip('"').strip("'").strip()
    return ""


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


def _cg_headers():
    key = clean_secret(os.environ.get("COINGECKO_API_KEY", ""))
    return {"x-cg-demo-api-key": key} if key else {}


def news_tag(title):
    t = title.lower()

    def has(words):
        return any(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", t) for w in words)
    if has(NEWS_NEG):
        return -1
    if has(NEWS_POS):
        return 1
    return 0


def _translate_vi(text):
    try:
        r = requests.get("https://translate.googleapis.com/translate_a/single",
                         params={"client": "gtx", "sl": "en", "tl": "vi", "dt": "t", "q": text[:500]},
                         headers=HTTP_HEADERS, timeout=10)
        r.raise_for_status()
        return "".join(p[0] for p in r.json()[0] if p and p[0]).strip() or text
    except Exception:
        return text


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


def etf_summary(edf, end=None):
    """Tóm tắt ETF NEAR đến ngày end (không nhìn trước)."""
    if edf is None or edf.empty:
        return None
    e = edf[edf.date <= end] if end is not None else edf
    if e.empty:
        return None
    last = e.iloc[-1]
    sign, streak = np.sign(last.total), 0
    for v in reversed(e.total.tolist()):
        if v != 0 and np.sign(v) == sign:
            streak += 1
        else:
            break
    fc = [c for c in e.columns if c.startswith("f_")]
    has = e[fc].notna().any(axis=1) if fc else pd.Series(False, index=e.index)
    fl = e[has].iloc[-1] if has.any() else last          # quỹ báo số từng quỹ muộn 1 phiên
    funds = {c[2:]: float(fl[c]) for c in fc if safe_float(fl[c]) not in (None, 0.0)}
    return {"date": last.date, "last": float(last.total), "sum5": float(e.total.tail(5).sum()),
            "sum20": float(e.total.tail(20).sum()), "aum": safe_float(last.aum), "cum": safe_float(last.cum),
            "streak": int(streak * (sign or 1)), "n": len(e), "funds": funds}


def _local_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def rsi(series, n=14):
    d = series.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


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


def _st_version():
    try:
        return tuple(int(x) for x in re.findall(r"\d+", st.__version__)[:2])
    except Exception:
        return (0, 0)


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


def _vn(ts):
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).astimezone(VN_TZ).strftime("%d/%m %H:%M")
    except Exception:
        return ts or "—"


NEWS_POS = ("partnership", "partners", "launch", "launches", "launched", "upgrade", "listing", "lists", "etf",
            "approval", "approved", "inflows", "adoption", "record", "surge", "surges", "rally", "rallies", "jumps",
            "soars", "breakout", "integration", "integrates", "milestone", "acquires", "bullish", "gains")
NEWS_NEG = ("hack", "hacked", "exploit", "outage", "delist", "delisting", "lawsuit", "sues", "unlock", "unlocks",
            "sell-off", "selloff", "plunge", "plunges", "drops", "falls", "slides", "tumbles", "outflows", "bearish",
            "downgrade", "halt", "halts", "investigation", "scam", "dump", "crash")


# =============================================================================
# NGUỒN DỮ LIỆU
# =============================================================================
def fetch_coingecko_history(days):
    """Giá, vốn hóa và tổng khối lượng (mọi sàn) theo ngày."""
    j = _get(f"https://api.coingecko.com/api/v3/coins/{LINK_CG_ID}/market_chart",
             params={"vs_currency": "usd", "days": int(days), "interval": "daily"}, headers=_cg_headers())
    cols = {}
    for key, name in (("prices", "price"), ("market_caps", "market_cap"), ("total_volumes", "volume_usd")):
        for ts, v in j.get(key, []) or []:
            d = _to_date(ts)
            if d is not None and safe_float(v) is not None:
                cols.setdefault(d, {})[name] = float(v)
    return cols


def fetch_coingecko_now():
    j = _get(f"https://api.coingecko.com/api/v3/coins/{LINK_CG_ID}",
             params={"localization": "false", "tickers": "false", "market_data": "true",
                     "community_data": "false", "developer_data": "false", "sparkline": "false"},
             headers=_cg_headers(), retries=0, timeout=15)
    md = j.get("market_data", {})
    return {"price": safe_float(md.get("current_price", {}).get("usd")),
            "change_24h": safe_float(md.get("price_change_percentage_24h")),
            "market_cap": safe_float(md.get("market_cap", {}).get("usd")),
            "volume_24h": safe_float(md.get("total_volume", {}).get("usd")),
            "circulating": safe_float(md.get("circulating_supply")), "source": "CoinGecko"}


def fetch_coinbase_now():
    j = _get("https://api.exchange.coinbase.com/products/LINK-USD/stats", retries=0, timeout=15)
    last, open_ = safe_float(j.get("last")), safe_float(j.get("open"))
    return {"price": last, "change_24h": (last / open_ - 1) * 100 if last and open_ else None, "source": "Coinbase"}


def fetch_link_derivatives():
    """Phái sinh LINK: OKX (OI, long/short, lực mua/bán chủ động, funding) + ảnh chụp Hyperliquid."""
    out, errors, now = {}, [], {}
    okx = "https://www.okx.com/api/v5"
    try:
        j = _get(f"{okx}/rubik/stat/contracts/open-interest-volume", params={"ccy": "LINK", "period": "1D"})
        for row in j.get("data", []) or []:
            d = _to_date(row[0])
            if d:
                out.setdefault(d, {}).update({"oi_usd": safe_float(row[1]), "perp_volume_usd": safe_float(row[2])})
    except Exception as e:
        errors.append(f"OKX OI: {e}")
    try:
        j = _get(f"{okx}/rubik/stat/contracts/long-short-account-ratio", params={"ccy": "LINK", "period": "1D"})
        for row in j.get("data", []) or []:
            d = _to_date(row[0])
            if d and safe_float(row[1]) is not None:
                out.setdefault(d, {})["long_short_ratio"] = float(row[1])
    except Exception as e:
        errors.append(f"OKX long/short: {e}")
    taker_err = []
    for path, prm in (("taker-volume-contract", {"instId": "LINK-USDT-SWAP", "period": "1D", "unit": "1"}),
                      ("taker-volume", {"ccy": "LINK", "instType": "CONTRACTS", "period": "1D"})):
        try:
            j = _get(f"{okx}/rubik/stat/{path}", params=prm)
            n = 0
            for row in j.get("data", []) or []:
                d = _to_date(row[0])
                sell, buy = safe_float(row[1]), safe_float(row[2])
                if d and sell is not None and buy is not None:
                    out.setdefault(d, {}).update({"taker_sell": sell, "taker_buy": buy})
                    n += 1
            if n:
                taker_err = []
                break
        except Exception as e:
            taker_err.append(f"OKX taker ({path}): {e}")
    errors += taker_err
    try:
        j = _get(f"{okx}/public/funding-rate-history", params={"instId": "LINK-USDT-SWAP", "limit": 100})
        rows = sorted((int(r["fundingTime"]), safe_float(r.get("realizedRate") or r.get("fundingRate")))
                      for r in j.get("data", []) or [] if r.get("fundingTime"))
        rows = [(t, v) for t, v in rows if v is not None]
        if len(rows) >= 2:
            interval_h = float(np.median(np.diff([t for t, _ in rows]) / 3.6e6)) or 8.0
            per_year = 24 * 365 / interval_h
            daily = {}
            for t, v in rows:
                daily.setdefault(_to_date(t), []).append(v)
            for d, vs in daily.items():
                out.setdefault(d, {})["funding_apr"] = float(np.mean(vs)) * per_year * 100
    except Exception as e:
        errors.append(f"OKX funding: {e}")
    try:
        r = requests.post("https://api.hyperliquid.xyz/info", json={"type": "metaAndAssetCtxs"}, timeout=20)
        r.raise_for_status()
        meta, ctxs = r.json()
        names = [u.get("name") for u in meta.get("universe", [])]
        if "LINK" in names:
            c = ctxs[names.index("LINK")]
            mark = safe_float(c.get("markPx"))
            now.update({"hl_funding_apr": (safe_float(c.get("funding")) or 0) * 24 * 365 * 100,
                        "hl_oi_usd": (safe_float(c.get("openInterest")) or 0) * (mark or 0),
                        "hl_volume_usd": safe_float(c.get("dayNtlVlm"))})
    except Exception as e:
        errors.append(f"Hyperliquid: {e}")
    return out, now, errors


def blockscout_holders(pages=HOLDER_PAGES):
    """~300 ví giữ nhiều LINK nhất, kèm nhãn (Binance, Kraken, Chainlink Staking…) từ Blockscout."""
    url, params, out = f"{BLOCKSCOUT}/api/v2/tokens/{LINK_TOKEN}/holders", None, []
    for _ in range(pages):
        r = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=30)
        r.raise_for_status()
        j = r.json()
        for it in j.get("items") or []:
            a = it.get("address") or {}
            tags = [t.get("name", "") for t in ((a.get("metadata") or {}).get("tags") or [])]
            bal = safe_float(it.get("value"))
            if a.get("hash") and bal is not None:
                out.append({"addr": a["hash"], "name": a.get("name") or "", "tags": "|".join(tags), "bal": bal / 1e18})
        params = j.get("next_page_params")
        if not params:
            break
        time.sleep(0.4)
    return out


def token_balance(addr):
    r = requests.get(f"{BLOCKSCOUT}/api", params={"module": "account", "action": "tokenbalance",
                                                   "contractaddress": LINK_TOKEN, "address": addr},
                     headers=HTTP_HEADERS, timeout=20)
    r.raise_for_status()
    v = safe_float(r.json().get("result"))
    if v is None:
        raise RuntimeError(f"Blockscout tokenbalance lỗi ({addr[:10]})")
    return v / 1e18


def exchange_brand(name, tags):
    """Nhãn → tên sàn (None nếu không phải ví sàn)."""
    txt = f"{name or ''}|{tags or ''}"
    if not tags or NOT_EXCHANGE_RE.search(txt) or not EXCHANGE_RE.search(txt):
        return None
    low = txt.lower()
    for b in EXCHANGE_BRANDS:
        if b.lower() in low:
            return b
    return "Sàn khác"


def update_exchange_staking(data, errors, max_extra=40):
    """Chụp số dư LINK của các ví sàn + pool staking. Dòng nạp/rút sàn chỉ tính trên các ví có mặt ở cả 2 lần chụp
    (ví mới lọt top không bị tính nhầm là nạp)."""
    holders = blockscout_holders()
    by_addr = {h["addr"].lower(): h for h in holders}
    prev = data.get("ex_wallets") or {}
    wallets = {}
    for h in holders:
        b = exchange_brand(h["name"], h["tags"])
        if b:
            wallets[h["addr"].lower()] = {"brand": b, "bal": round(h["bal"], 2), "label": (h["tags"].split("|")[0])[:40]}
    n_extra = 0
    for a, w in prev.items():                    # ví sàn cũ rơi khỏi top → hỏi số dư riêng
        if a in wallets or n_extra >= max_extra:
            continue
        try:
            wallets[a] = {**w, "bal": round(token_balance(a), 2)}
            n_extra += 1
            time.sleep(0.25)
        except Exception as e:
            errors.append(f"Blockscout {a[:10]}: {redact(e)[:60]}")
    flow = sum(w["bal"] - prev[a]["bal"] for a, w in wallets.items() if a in prev)
    brands = {}
    for w in wallets.values():
        brands[w["brand"]] = round(brands.get(w["brand"], 0) + w["bal"], 2)
    stake = {}
    for k, addr in {**STAKING_POOLS, "reward_vault": REWARD_VAULT}.items():
        h = by_addr.get(addr.lower())
        try:
            stake[k] = h["bal"] if h else token_balance(addr)
        except Exception as e:
            errors.append(f"staking {k}: {redact(e)[:60]}")
    data["ex_wallets"] = wallets
    return {"ex_total": round(sum(w["bal"] for w in wallets.values()), 2), "ex_n": len(wallets),
            "ex_brands": brands, "flow": round(flow, 2) if prev else None,
            "stake_community": stake.get("community"), "stake_operator": stake.get("operator"),
            "stake_total": (stake.get("community") or 0) + (stake.get("operator") or 0) or None,
            "reward_vault": stake.get("reward_vault"),
            "top_holders_total": round(sum(h["bal"] for h in holders), 2)}


def fetch_link_news(days=7, limit=25, translate=True):
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    from urllib.parse import quote_plus
    cutoff = utc_now() - timedelta(days=days)
    rows, seen = [], set()
    for q in LINK_NEWS_QUERIES:
        try:
            r = requests.get("https://news.google.com/rss/search?q=" + quote_plus(q) + "&hl=en-US&gl=US&ceid=US:en",
                             headers=HTTP_HEADERS, timeout=15)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception:
            continue
        for item in root.findall("./channel/item"):
            title = (item.findtext("title") or "").strip()
            if not title or title.lower() in seen:
                continue
            try:
                pub = parsedate_to_datetime(item.findtext("pubDate") or "").astimezone(timezone.utc)
            except Exception:
                continue
            if pub < cutoff:
                continue
            seen.add(title.lower())
            src = item.find("source")
            rows.append({"title": title, "link": (item.findtext("link") or "").strip(),
                         "source": (src.text or "").strip() if src is not None else "Google News",
                         "published": pub.strftime("%Y-%m-%d %H:%M"), "tag": news_tag(title)})
    rows.sort(key=lambda x: x["published"], reverse=True)
    rows = rows[:limit]
    if translate:
        for r in rows:
            r["title_vi"] = _translate_vi(r["title"])
    return rows


def link_etf_frame(etf_data):
    """ETF LINK giao ngay tại Mỹ (GLNK, CLNK…) từ file ETF của app BTC."""
    d = ((etf_data or {}).get("alts") or {}).get("LINK") or {}
    rows = []
    for k, rec in (d.get("daily") or {}).items():
        if is_day_key(k) and isinstance(rec, dict) and safe_float(rec.get("total")) is not None:
            rows.append({"date": parse_day(k), "total": float(rec["total"]), "aum": safe_float(rec.get("aum")),
                         "cum": safe_float(rec.get("cum")),
                         **{f"f_{t}": safe_float(v) for t, v in (rec.get("funds") or {}).items()}})
    if not rows:
        return pd.DataFrame(columns=["date", "total", "aum", "cum"])
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


# =============================================================================
# CẬP NHẬT (GitHub Actions)
# =============================================================================
def _merge(daily, rows):
    n = 0
    for d, rec in rows.items():
        key = day_str(d) if not isinstance(d, str) else d
        if is_day_key(key):
            daily.setdefault(key, {}).update({k: v for k, v in rec.items() if v is not None})
            n += 1
    return n


def run_link_update():
    data = _local_json(LINK_FILE)
    daily = data.setdefault("daily", {})
    errors, done = {}, {}
    today = utc_today()
    have = [k for k, v in daily.items() if isinstance(v, dict) and v.get("price")]
    try:
        rows = fetch_coingecko_history(365 if len(have) < 300 else 30)
        rows.pop(today, None)
        done["giá"] = _merge(daily, rows)
    except Exception as e:
        errors["coingecko"] = redact(e)[:200]
    rows, now, errs = fetch_link_derivatives()
    done["phái sinh"] = _merge(daily, rows)
    if now:
        daily.setdefault(day_str(today), {}).update(now)
    if errs:
        errors["phái sinh"] = " | ".join(errs)[:300]
    ex_errs = []
    try:
        snap = update_exchange_staking(data, ex_errs)
        rec = daily.setdefault(day_str(today), {})
        flow = snap.pop("flow")
        if flow is not None:                     # cộng dồn dòng nạp/rút của các lần chạy trong ngày
            if rec.get("ex_flow_runs_day") != day_str(today):
                rec["ex_flow"], rec["ex_flow_runs_day"] = 0.0, day_str(today)
            rec["ex_flow"] = round((rec.get("ex_flow") or 0) + flow, 2)
        rec.update({k: v for k, v in snap.items() if v is not None})
        done["sàn & staking"] = snap.get("ex_n")
    except Exception as e:
        ex_errs.append(redact(e)[:200])
    if ex_errs:
        errors["sàn & staking"] = "; ".join(ex_errs[:4])
    try:
        news = fetch_link_news()
        if news:
            data["news"] = news
            done["tin"] = len(news)
    except Exception as e:
        errors["tin"] = redact(e)[:200]
    data["daily"] = dict(sorted((k, v) for k, v in daily.items() if is_day_key(k))[-HISTORY_DAYS:])
    data["status"] = {"last_run_utc": utc_now().strftime("%Y-%m-%d %H:%M"), "updated": done, "errors": errors}
    with open(LINK_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    _log(f"cập nhật {done}")
    for k, v in errors.items():
        _log(f"  ⚠️ {k}: {v}")
    try:
        write_link_report(data)
    except Exception as e:
        _log(f"  ⚠️ daily_report.json: {e}")


# =============================================================================
# PHÂN TÍCH
# =============================================================================
def _score_of(light):
    return {"🟢": 1, "🔴": -1, "🟡": 0}.get(light)


def flows_summary(df, days):
    """Nạp/rút ròng lên sàn (LINK) và thay đổi staking trong N ngày gần nhất."""
    if df is None or df.empty:
        return {}
    end = df.date.max()
    w = df[df.date > end - timedelta(days=days)]
    out = {"ex_flow": float(w["ex_flow"].sum()) if "ex_flow" in w and w["ex_flow"].notna().any() else None,
           "ex_days": int(w["ex_flow"].notna().sum()) if "ex_flow" in w else 0}
    for col in ("stake_total", "ex_total"):
        if col in df:
            s = df[["date", col]].dropna()
            if s.empty:
                continue
            cur = s.iloc[-1]
            out[f"{col}_now"] = float(cur[col])
            if len(s) >= 2:
                old = s[s.date <= cur.date - timedelta(days=days)]
                base = old.iloc[-1][col] if not old.empty else s.iloc[0][col]
                out[f"{col}_chg"] = float(cur[col] - base)
    return out


def compute_link_report(df, price, mm, etf_sum, news):
    if df is None or df.empty or "price" not in df:
        return None
    p = df.set_index("date")["price"].dropna()
    if len(p) < 60:
        return None
    price = price or float(p.iloc[-1])
    e20, e50 = float(ema(p, 20).iloc[-1]), float(ema(p, 50).iloc[-1])
    ch7 = (price / float(p.iloc[-8]) - 1) * 100 if len(p) > 8 else None
    r14 = float(rsi(p).iloc[-1])
    cats = []

    def cat(name, sigs):
        sigs = [s for s in sigs if s[1] != "⚪"]
        vals = [_score_of(s[1]) for s in sigs]
        cats.append({"name": name, "weight": REPORT_WEIGHTS[name],
                     "score": float(np.mean(vals)) if vals else None,
                     "signals": [{"label": s[0], "light": s[1], "detail": s[2]} for s in sigs]})

    vs50 = (price / e50 - 1) * 100
    cb = mm.get("realized_price") if mm else None
    cat("Giá & xu hướng", [
        ("Giá so với EMA50", "🟢" if vs50 > 0 else ("🔴" if vs50 < -10 else "🟡"), f"{vs50:+.1f}%"),
        ("EMA20 so với EMA50", "🟢" if e20 > e50 else "🔴", "EMA20 trên EMA50" if e20 > e50 else "EMA20 dưới EMA50"),
        ("Biến động 7 ngày", "⚪" if ch7 is None else ("🟢" if ch7 > 5 else ("🔴" if ch7 < -5 else "🟡")),
         f"{ch7:+.1f}%" if ch7 is not None else "N/A"),
        ("RSI 14 ngày", "🟢" if r14 < 30 else ("🔴" if r14 > 70 else "🟡"), f"{r14:.0f}"),
        ("Giá so với giá vốn ước tính", "⚪" if not cb else ("🟢" if price > cb else ("🔴" if price < cb * 0.85 else "🟡")),
         f"{(price / cb - 1) * 100:+.1f}% (giá vốn {fmt_price(cb)})" if cb else "N/A"),
    ])
    fund, _ = last_val(df, "funding_apr")
    ls, _ = last_val(df, "long_short_ratio")
    oi_chg = None
    if "oi_usd" in df:
        o = df[["date", "oi_usd", "price"]].dropna()
        if len(o) > 8:
            coin_now, coin_old = o.oi_usd.iloc[-1] / o.price.iloc[-1], o.oi_usd.iloc[-8] / o.price.iloc[-8]
            oi_chg = (coin_now / coin_old - 1) * 100 if coin_old else None
    tk = None
    if {"taker_buy", "taker_sell"}.issubset(df.columns):
        t7 = df[["taker_buy", "taker_sell"]].dropna().tail(7)
        tk = float(t7.taker_buy.sum() / t7.taker_sell.sum()) if len(t7) and t7.taker_sell.sum() else None
    cat("Phái sinh", [
        ("Funding (năm hóa)", "⚪" if fund is None else ("🔴" if fund > 15 else ("🟢" if fund < 0 else "🟡")),
         f"{fund:+.1f}%" if fund is not None else "N/A"),
        ("Open interest 7 ngày (tính bằng LINK)", "⚪" if oi_chg is None else
         ("🔴" if oi_chg > 10 and (ch7 or 0) < 0 else ("🟢" if oi_chg > 10 and (ch7 or 0) > 0 else "🟡")),
         f"{oi_chg:+.0f}%" if oi_chg is not None else "N/A"),
        ("Tỷ lệ tài khoản long/short", "⚪" if ls is None else ("🔴" if ls > 2 else ("🟢" if ls < 1 else "🟡")),
         f"{ls:.2f}" if ls is not None else "N/A"),
        ("Lực mua/bán chủ động 7 ngày", "⚪" if tk is None else ("🟢" if tk > 1.03 else ("🔴" if tk < 0.97 else "🟡")),
         f"{tk:.2f}" if tk else "N/A"),
    ])
    f7 = flows_summary(df, 7)
    exf, ext = f7.get("ex_flow"), f7.get("ex_total_now")
    thr = (ext or 0) * 0.002
    stc = f7.get("stake_total_chg")
    cat("Dòng tiền (ETF · sàn · staking)", [
        ("ETF LINK (Mỹ) — 5 phiên", "⚪" if not etf_sum else ("🟢" if etf_sum["sum5"] > 0 else ("🔴" if etf_sum["sum5"] < 0 else "🟡")),
         (f"{fmt_big_usd(etf_sum['sum5'], signed=True)}" + (f" ({etf_sum['sum5'] / etf_sum['aum'] * 100:+.1f}% tổng tài sản)"
                                                           if etf_sum.get("aum") else "")) if etf_sum else "N/A"),
        ("Nạp/rút ròng ví sàn 7 ngày", "⚪" if exf is None or f7.get("ex_days", 0) < 2 else
         ("🔴" if exf > thr else ("🟢" if exf < -thr else "🟡")),
         (f"{fmt_link(exf, signed=True)} ({'nạp lên sàn' if exf > 0 else 'rút khỏi sàn'})" if exf is not None else "chưa đủ dữ liệu")),
        ("Staking Chainlink 7 ngày", "⚪" if stc is None else ("🟢" if stc > 1000 else ("🔴" if stc < -1000 else "🟡")),
         fmt_link(stc, signed=True) if stc is not None else "chưa đủ dữ liệu"),
    ])
    vol = None
    if "volume_usd" in df:
        v = df[["date", "volume_usd"]].dropna()
        if len(v) > 31:
            vol = (v.volume_usd.tail(7).mean() / v.volume_usd.tail(30).mean() - 1) * 100
    cat("Khối lượng", [("Khối lượng 7N so với 30N", "⚪" if vol is None else
                        ("🟢" if vol > 20 and (ch7 or 0) > 0 else ("🔴" if vol > 20 and (ch7 or 0) < 0 else "🟡")),
                        f"{vol:+.0f}%" if vol is not None else "N/A")])
    cut = (utc_now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
    recent = [n for n in (news or []) if n.get("published", "") >= cut]
    pos, neg = sum(1 for n in recent if n.get("tag") == 1), sum(1 for n in recent if n.get("tag") == -1)
    cat("Tin tức", [("Tin 3 ngày (gắn nhãn theo từ khóa)", "🟢" if pos > neg + 1 else ("🔴" if neg > pos + 1 else "🟡"),
                     f"{pos} tích cực · {neg} tiêu cực · {len(recent) - pos - neg} trung tính")])
    used = [c for c in cats if c["score"] is not None]
    wsum = sum(c["weight"] for c in used) or 1
    score = 50 + 50 * sum(c["weight"] * c["score"] for c in used) / wsum
    verdict = ("🟢 Tích cực" if score >= 65 else "🟢 Hơi tích cực" if score >= 55 else "🟡 Trung tính" if score >= 45
               else "🟠 Hơi tiêu cực" if score >= 35 else "🔴 Tiêu cực")
    return {"score": score, "verdict": verdict, "categories": cats, "ema20": e20, "ema50": e50, "rsi": r14,
            "change_7d": ch7, "price": price}


def ma_trend(df, price):
    p = df.set_index("date")["price"].dropna()
    out = {}
    for n in (50, 200):
        if len(p) >= n:
            out[f"ma{n}"] = float(p.tail(n).mean())
            out[f"vs_ma{n}"] = (price / out[f"ma{n}"] - 1) * 100
    for d in (7, 30, 90, 365):
        if len(p) > d:
            out[f"chg_{d}d"] = (price / float(p.iloc[-d - 1]) - 1) * 100
    return out


def latest_brands(data):
    daily = data.get("daily") or {}
    for k in sorted(daily, reverse=True):
        b = (daily.get(k) or {}).get("ex_brands")
        if isinstance(b, dict) and b:
            return b
    return {}


def write_link_report(data):
    """Ghi phần LINK vào daily_report.json để báo cáo nhanh theo yêu cầu."""
    df = daily_frame(data)
    if df.empty or "price" not in df:
        return
    live = {}
    for fn in (fetch_coingecko_now, fetch_coinbase_now):
        try:
            live = fn()
            if live.get("price"):
                break
        except Exception:
            continue
    price = live.get("price") or float(df.price.dropna().iloc[-1])
    qty = safe_float(clean_secret(os.environ.get("LINK_HOLDINGS", ""))) or 450.0
    model = cost_basis_model(df)
    mm = model_metrics(model, price)
    sup, res = find_zones(model, price)
    etf = etf_summary(link_etf_frame(_local_json(ETF_FILE)))
    rep = compute_link_report(df, price, mm, etf, data.get("news"))
    f1, f7, f30 = flows_summary(df, 1), flows_summary(df, 7), flows_summary(df, 30)
    r = lambda v, n=2: round(float(v), n) if v is not None and np.isfinite(v) else None
    out = {"generated_utc": utc_now().strftime("%Y-%m-%d %H:%M"), "price_live": r(price, 4),
           "change_24h": live.get("change_24h"), "holdings": qty, "holdings_value_usd": r(qty * price),
           "score": r(rep["score"], 0) if rep else None, "verdict": rep["verdict"] if rep else None,
           "categories": [{"name": c["name"], "score": None if c["score"] is None else r(c["score"]),
                           "signals": [f"{s['light']} {s['label']}: {s['detail']}" for s in c["signals"]]}
                          for c in (rep or {}).get("categories", [])],
           "cost_basis_est": r(mm.get("realized_price"), 4), "supply_in_loss_est_pct": r(mm.get("loss_pct"), 1),
           "supports": [f"{z['low']:.2f}-{z['high']:.2f}" for z in sup], "resistances": [f"{z['low']:.2f}-{z['high']:.2f}" for z in res],
           "trend": {k: r(v) for k, v in ma_trend(df, price).items()},
           "etf": ({k: (str(v) if k == "date" else v) for k, v in etf.items()} if etf else None),
           "exchange": {"total_now": r(f7.get("ex_total_now"), 0), "flow_1d": r(f1.get("ex_flow"), 0),
                        "flow_7d": r(f7.get("ex_flow"), 0), "flow_30d": r(f30.get("ex_flow"), 0),
                        "days_tracked": f30.get("ex_days"),
                        "brands": {k: round(v) for k, v in sorted(latest_brands(data).items(), key=lambda kv: -kv[1])[:8]}},
           "staking": {"total_now": r(f7.get("stake_total_now"), 0), "chg_7d": r(f7.get("stake_total_chg"), 0),
                       "chg_30d": r(f30.get("stake_total_chg"), 0)},
           "derivatives": {k: r(last_val(df, k)[0]) for k in ("funding_apr", "long_short_ratio", "oi_usd", "hl_funding_apr")},
           "news_top": [f"{n['published']} {['🔴', '⚪', '🟢'][n.get('tag', 0) + 1]} {n.get('title_vi') or n['title']}"
                        for n in (data.get("news") or [])[:6]]}
    rep_all = _local_json(REPORT_FILE)
    rep_all["link"] = out
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(rep_all, f, ensure_ascii=False, indent=1, default=str)
    _log("daily_report.json: đã ghi phần LINK")


# =============================================================================
# DASHBOARD
# =============================================================================
def inject_css():
    st.markdown("""
<style>
.block-container {padding-top: 2rem; max-width: 1500px;}
[data-testid="stMetric"] {background: linear-gradient(135deg, rgba(20,30,60,.95), rgba(15,23,42,.95));
  border: 1px solid rgba(55,91,210,.35); padding: 14px 18px; border-radius: 14px;}
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] * {font-size: .88rem; color: #cbd5e1 !important;}
[data-testid="stMetricValue"] {font-size: 1.45rem; color: #f8fafc !important;}
</style>""", unsafe_allow_html=True)


STRETCH = {"width": "stretch"} if _st_version() >= (1, 50) else {"use_container_width": True}


@st.cache_data(ttl=900, show_spinner=False)
def load_link():
    data, src = read_data_file(LINK_FILE)
    return data, src


@st.cache_data(ttl=900, show_spinner=False)
def load_link_etf():
    data, _ = read_data_file(ETF_FILE)
    return link_etf_frame(data)


@st.cache_data(ttl=300, show_spinner=False)
def live_link():
    for fn in (fetch_coingecko_now, fetch_coinbase_now):
        try:
            v = fn()
            if v.get("price"):
                return v
        except Exception:
            continue
    return {}


def main_link():
    inject_css()
    st.title("🔗 LINK Monitor — Chainlink")
    data, src = load_link()
    df = daily_frame(data)
    if df.empty or "price" not in df:
        st.info("Chưa có dữ liệu LINK. Chạy workflow **Update BTC data** (bước 'Lấy dữ liệu LINK').")
        return
    live = live_link()
    price = live.get("price") or float(df.price.dropna().iloc[-1])
    try:
        default_qty = float(_secret("LINK_HOLDINGS") or 450)
    except ValueError:
        default_qty = 450.0
    qty = st.sidebar.number_input("Số LINK bạn giữ", min_value=0.0, value=default_qty, step=10.0, key="link_qty")
    model = cost_basis_model(df)
    mm = model_metrics(model, price)
    sup, res = find_zones(model, price)
    edf = load_link_etf()
    es = etf_summary(edf)
    rep = compute_link_report(df, price, mm, es, data.get("news"))
    tr = ma_trend(df, price)
    f1, f7, f30 = flows_summary(df, 1), flows_summary(df, 7), flows_summary(df, 30)

    m = st.columns(5)
    m[0].metric("Giá LINK", fmt_price(price), f"{live['change_24h']:+.2f}% 24h" if live.get("change_24h") is not None else None)
    m[1].metric(f"Vị thế {qty:,.0f} LINK", fmt_big_usd(qty * price))
    m[2].metric("Điểm tổng hợp", f"{rep['score']:.0f}/100" if rep else "N/A", rep["verdict"] if rep else None, delta_color="off")
    m[3].metric("Giá vốn ước tính thị trường", fmt_price(mm.get("realized_price")),
                f"{mm['loss_pct']:.0f}% cung đang lỗ" if mm.get("loss_pct") is not None else None, delta_color="off")
    m[4].metric("Đang stake (Chainlink v0.2)", fmt_link(f7.get("stake_total_now")),
                fmt_link(f7.get("stake_total_chg"), signed=True) + " / 7 ngày" if f7.get("stake_total_chg") is not None else None,
                delta_color="off")
    cap(f"{LINK_VERSION} · dữ liệu {src} · cập nhật {_vn((data.get('status') or {}).get('last_run_utc'))} giờ VN. "
        "Tham khảo, không phải khuyến nghị đầu tư.")

    tabs = st.tabs(["📊 Tổng quan", "🏦 Dòng tiền (ETF · sàn)", "🔒 Staking", "📈 Phái sinh", "📰 Tin tức"])
    with tabs[0]:
        tab_overview(df, price, rep, tr, sup, res)
    with tabs[1]:
        tab_flows(df, edf, es, f1, f7, f30, data)
    with tabs[2]:
        tab_staking(df)
    with tabs[3]:
        tab_derivs(df)
    with tabs[4]:
        tab_news(data.get("news") or [])


def tab_overview(df, price, rep, tr, sup, res):
    if rep:
        mk(f"### {rep['verdict']} — {rep['score']:.0f}/100")
        rows = []
        for c in rep["categories"]:
            icon = "⚪" if c["score"] is None else ("🟢" if c["score"] > 0.2 else ("🔴" if c["score"] < -0.2 else "🟡"))
            rows.append({"Nhóm": c["name"], "Đèn": icon, "Trọng số": f"{c['weight'] * 100:.0f}%",
                         "Tín hiệu": " · ".join(f"{s['light']} {s['label']}: {s['detail']}" for s in c["signals"])})
        show_df(pd.DataFrame(rows))
    m = st.columns(4)
    m[0].metric("MA50", fmt_price(tr.get("ma50")), f"giá {tr['vs_ma50']:+.1f}%" if tr.get("vs_ma50") is not None else None)
    m[1].metric("MA200", fmt_price(tr.get("ma200")), f"giá {tr['vs_ma200']:+.1f}%" if tr.get("vs_ma200") is not None else None)
    m[2].metric("30 ngày", f"{tr['chg_30d']:+.1f}%" if tr.get("chg_30d") is not None else "N/A")
    m[3].metric("365 ngày", f"{tr['chg_365d']:+.1f}%" if tr.get("chg_365d") is not None else "N/A")
    days = st.radio("Khoảng thời gian", [90, 180, 365, 730], index=2, horizontal=True,
                    format_func=lambda x: f"{x} ngày", key="link_days")
    p = df[["date", "price"]].dropna().copy()
    p["ma50"], p["ma200"] = p.price.rolling(50).mean(), p.price.rolling(200).mean()
    w = p[p.date >= p.date.max() - timedelta(days=days)]
    fig = go.Figure()
    fig.add_scatter(x=w.date, y=w.price, name="Giá LINK", line=dict(color="#f8fafc", width=2))
    fig.add_scatter(x=w.date, y=w.ma50, name="MA50", line=dict(color=AMBER, width=1.6))
    fig.add_scatter(x=w.date, y=w.ma200, name="MA200", line=dict(color=LINK_BLUE, width=2))
    for z in sup:
        fig.add_hrect(y0=z["low"], y1=z["high"], fillcolor=GREEN, opacity=0.12, line_width=0)
    for z in res:
        fig.add_hrect(y0=z["low"], y1=z["high"], fillcolor=RED, opacity=0.12, line_width=0)
    style_fig(fig, 480)
    fig.update_yaxes(tickprefix="$")
    show_fig(fig, "link_price")
    c = st.columns(2)
    with c[0]:
        mk("**🟢 Hỗ trợ (cụm giá vốn ước tính dưới giá)**")
        show_df(pd.DataFrame([{"Vùng": f"{fmt_price(z['low'])} – {fmt_price(z['high'])}", "Cách giá": f"{z['distance_pct']:+.1f}%"}
                              for z in sup]) if sup else pd.DataFrame())
    with c[1]:
        mk("**🔴 Kháng cự (cụm giá vốn ước tính trên giá)**")
        show_df(pd.DataFrame([{"Vùng": f"{fmt_price(z['low'])} – {fmt_price(z['high'])}", "Cách giá": f"{z['distance_pct']:+.1f}%"}
                              for z in res]) if res else pd.DataFrame())
    cap("Giá vốn ước tính dựng từ giá × khối lượng giao dịch (mô hình gần đúng, không phải on-chain thật như BTC).")


def tab_flows(df, edf, es, f1, f7, f30, data):
    st.subheader("🏦 ETF LINK giao ngay (Mỹ)")
    if edf is None or edf.empty:
        st.info("Chưa có dữ liệu ETF LINK (app BTC lấy từ SoSoValue).")
    else:
        m = st.columns(4)
        m[0].metric(f"Phiên {es['date']:%d/%m}", fmt_big_usd(es["last"], signed=True))
        m[1].metric("5 phiên", fmt_big_usd(es["sum5"], signed=True))
        m[2].metric("20 phiên", fmt_big_usd(es["sum20"], signed=True))
        m[3].metric("Tổng tài sản", fmt_big_usd(es.get("aum")))
        e = edf.tail(60)
        fig = go.Figure()
        fig.add_bar(x=e.date, y=e.total, marker_color=[GREEN if v >= 0 else RED for v in e.total], name="Dòng tiền/phiên")
        style_fig(fig, 320)
        fig.update_yaxes(tickprefix="$")
        show_fig(fig, "link_etf")
        if es.get("funds"):
            cap("Phiên gần nhất theo quỹ: " + " · ".join(f"{k} {fmt_big_usd(v, signed=True)}" for k, v in es["funds"].items()))

    st.subheader("🔁 LINK nạp/rút trên các ví sàn")
    m = st.columns(4)
    m[0].metric("Đang nằm trên ví sàn", fmt_link(f7.get("ex_total_now")))
    m[1].metric("Ròng 24h", fmt_link(f1.get("ex_flow"), signed=True) if f1.get("ex_flow") is not None else "N/A",
                "nạp lên sàn" if (f1.get("ex_flow") or 0) > 0 else "rút khỏi sàn", delta_color="inverse")
    m[2].metric("Ròng 7 ngày", fmt_link(f7.get("ex_flow"), signed=True) if f7.get("ex_flow") is not None else "N/A",
                "áp lực bán" if (f7.get("ex_flow") or 0) > 0 else "rút về giữ", delta_color="inverse")
    m[3].metric("Ròng 30 ngày", fmt_link(f30.get("ex_flow"), signed=True) if f30.get("ex_flow") is not None else "N/A")
    if "ex_flow" in df and df.ex_flow.notna().any():
        w = df[["date", "ex_flow", "ex_total"]].dropna(subset=["ex_flow"]).tail(90)
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_bar(x=w.date, y=w.ex_flow, name="Nạp(+)/rút(−) ròng mỗi ngày",
                    marker_color=[RED if v > 0 else GREEN for v in w.ex_flow], secondary_y=False)
        fig.add_scatter(x=w.date, y=w.ex_total, name="Tổng LINK trên ví sàn", mode="lines",
                        line=dict(color=CYAN, width=2), secondary_y=True)
        style_fig(fig, 360)
        show_fig(fig, "link_exflow")
    else:
        st.info("Đang bắt đầu ghi — cần ít nhất 2 lần chạy để có số nạp/rút.")
    brands = latest_brands(data)
    if brands:
        show_df(pd.DataFrame([{"Sàn": k, "LINK đang giữ": f"{v:,.0f}"} for k, v in sorted(brands.items(), key=lambda kv: -kv[1])]))
    cap("Cách tính: tổng số dư LINK của các ví có nhãn sàn (Binance, Kraken, Coinbase, OKX…) trong ~300 ví giữ nhiều LINK "
        "nhất (Blockscout). Nạp/rút chỉ tính trên ví có mặt ở cả 2 lần chụp, nên ví mới lọt top không bị tính nhầm. "
        "Nạp lên sàn = có thể bán; rút khỏi sàn = gom/giữ. Chỉ là tín hiệu.")


def tab_staking(df):
    st.subheader("🔒 Chainlink Staking v0.2")
    if "stake_total" not in df or df.stake_total.dropna().empty:
        st.info("Đang bắt đầu ghi số dư pool staking.")
        return
    s = df[["date", "stake_community", "stake_operator", "stake_total", "reward_vault"]].dropna(subset=["stake_total"])
    last = s.iloc[-1]
    m = st.columns(4)
    m[0].metric("Pool cộng đồng", fmt_link(last.stake_community))
    m[1].metric("Pool node operator", fmt_link(last.stake_operator))
    m[2].metric("Tổng đang stake", fmt_link(last.stake_total))
    m[3].metric("Kho thưởng (Reward Vault)", fmt_link(last.reward_vault))
    fig = go.Figure()
    fig.add_scatter(x=s.date, y=s.stake_community, name="Pool cộng đồng", stackgroup="one", line=dict(color=LINK_BLUE))
    fig.add_scatter(x=s.date, y=s.stake_operator, name="Pool node operator", stackgroup="one", line=dict(color=CYAN))
    style_fig(fig, 360)
    show_fig(fig, "link_staking")
    cap("Staking tăng = LINK bị khóa thêm (bớt nguồn cung bán); giảm = người stake rút ra (có thể bán). "
        "Pool cộng đồng thường gần đầy hạn mức nên thay đổi nhỏ; để ý khi giảm mạnh.")


def tab_derivs(df):
    st.subheader("📈 Phái sinh LINK (OKX · Hyperliquid)")
    d = df[df.date >= df.date.max() - timedelta(days=120)]
    m = st.columns(4)
    for i, (col, lab, fmt) in enumerate((("funding_apr", "Funding OKX (năm hóa)", lambda v: f"{v:+.1f}%"),
                                         ("hl_funding_apr", "Funding Hyperliquid", lambda v: f"{v:+.1f}%"),
                                         ("long_short_ratio", "Long/short (tài khoản)", lambda v: f"{v:.2f}"),
                                         ("oi_usd", "Open interest OKX", fmt_big_usd))):
        v, _ = last_val(df, col)
        m[i].metric(lab, fmt(v) if v is not None else "N/A")
    if "oi_usd" in d:
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_scatter(x=d.date, y=d.oi_usd, name="OI (USD)", line=dict(color=AMBER), secondary_y=False)
        fig.add_scatter(x=d.date, y=d.price, name="Giá", line=dict(color="#f8fafc"), secondary_y=True)
        style_fig(fig, 320)
        show_fig(fig, "link_oi")
    cols = [c for c in ("funding_apr", "long_short_ratio") if c in d]
    if cols:
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        if "funding_apr" in d:
            fig.add_bar(x=d.date, y=d.funding_apr, name="Funding %/năm", marker_color=GREY, secondary_y=False)
        if "long_short_ratio" in d:
            fig.add_scatter(x=d.date, y=d.long_short_ratio, name="Long/short", line=dict(color=CYAN), secondary_y=True)
        style_fig(fig, 300)
        show_fig(fig, "link_fund")
    cap("Long/short > 2 và funding cao = đám đông nghiêng long (dễ bị quét). OI tăng khi giá giảm = vị thế short mới.")


def tab_news(news):
    st.subheader("📰 Tin Chainlink 7 ngày")
    if not news:
        st.info("Chưa có tin.")
        return
    for n in news[:25]:
        icon = {1: "🟢", -1: "🔴"}.get(n.get("tag"), "⚪")
        mk(f"{icon} **{_vn(n['published'])}** · [{n.get('title_vi') or n['title']}]({n['link']}) — _{n.get('source', '')}_")


if UPDATE_MODE:
    run_link_update()
else:
    main_link()
