"""URPD Profit / Accumulation module.
Green = profit-taking proxy; red = accumulation proxy.
"""
from __future__ import annotations
from typing import Optional
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st


def build_flow_table(current: pd.DataFrame, previous: pd.DataFrame, current_price: float) -> pd.DataFrame:
    req = {"price_low", "price_high", "btc_amount"}
    if not req.issubset(current.columns) or not req.issubset(previous.columns):
        raise ValueError(f"URPD data must contain: {sorted(req)}")
    key = ["price_low", "price_high"]
    a = current[key + ["btc_amount"]].copy()
    b = previous[key + ["btc_amount"]].copy()
    for df in (a, b):
        for col in key + ["btc_amount"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    a = a.groupby(key, as_index=False)["btc_amount"].sum()
    b = b.groupby(key, as_index=False)["btc_amount"].sum()
    out = a.merge(b, on=key, how="outer", suffixes=("_current", "_previous")).fillna(0.0)
    out["mid_price"] = (out["price_low"] + out["price_high"]) / 2
    out["delta_btc"] = out["btc_amount_current"] - out["btc_amount_previous"]
    out["signal"] = np.select(
        [
            (out["delta_btc"] > 0) & (out["mid_price"] <= current_price),
            (out["delta_btc"] > 0) & (out["mid_price"] > current_price),
            (out["delta_btc"] < 0) & (out["mid_price"] <= current_price),
            (out["delta_btc"] < 0) & (out["mid_price"] > current_price),
        ],
        ["Gom thêm (proxy)", "Tái phân bổ / chưa rõ", "Chốt lời (proxy)", "Giảm cung phía trên / chưa rõ"],
        default="Không đổi",
    )
    return out.sort_values("mid_price").reset_index(drop=True)


def render_profit_accumulation(current: pd.DataFrame, previous: Optional[pd.DataFrame], current_price: float) -> None:
    st.subheader("Dòng BTC theo giá vốn — Chốt lời / Gom thêm")
    st.caption("🟢 Chốt lời và 🔴 Gom thêm là tín hiệu suy ra từ thay đổi URPD giữa hai snapshot; không phải xác nhận giao dịch của từng ví.")
    if previous is None or previous.empty:
        st.info("Cần ít nhất 2 snapshot URPD khác ngày để tính dòng BTC.")
        return
    flow = build_flow_table(current, previous, current_price)
    colors = np.where(flow["delta_btc"] >= 0, "#ef4444", "#10b981")
    fig = go.Figure(go.Bar(
        x=flow["mid_price"], y=flow["delta_btc"],
        width=(flow["price_high"] - flow["price_low"]) * 0.9,
        marker_color=colors,
        customdata=flow[["price_low", "price_high", "signal"]].to_numpy(),
        hovertemplate="Khoảng giá: $%{customdata[0]:,.0f} – $%{customdata[1]:,.0f}<br>Thay đổi: %{y:,.0f} BTC<br>Tín hiệu: %{customdata[2]}<extra></extra>",
    ))
    fig.add_hline(y=0, line_width=1)
    fig.add_vline(x=current_price, line_dash="dash", annotation_text=f"Giá hiện tại ${current_price:,.0f}")
    fig.update_layout(height=480, xaxis_title="Giá vốn bucket (USD)", yaxis_title="Δ BTC", margin=dict(l=20, r=20, t=30, b=20))
    st.plotly_chart(fig, use_container_width=True)
    profit = -flow.loc[(flow["delta_btc"] < 0) & (flow["mid_price"] <= current_price), "delta_btc"].sum()
    accumulation = flow.loc[flow["delta_btc"] > 0, "delta_btc"].sum()
    net = accumulation - profit
    c1, c2, c3 = st.columns(3)
    c1.metric("🟢 Chốt lời (proxy)", f"{profit:,.0f} BTC")
    c2.metric("🔴 Gom thêm (proxy)", f"{accumulation:,.0f} BTC")
    c3.metric("Dòng ròng", f"{net:+,.0f} BTC")
    st.dataframe(flow[["price_low", "price_high", "btc_amount_current", "btc_amount_previous", "delta_btc", "signal"]].rename(columns={
        "price_low":"Giá thấp", "price_high":"Giá cao", "btc_amount_current":"BTC hiện tại",
        "btc_amount_previous":"BTC trước", "delta_btc":"Δ BTC", "signal":"Diễn giải"
    }), hide_index=True, use_container_width=True)
