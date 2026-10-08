                    for r in dec7:
                        st.write("• " + _bucket_text(r))
            else:
                st.write("Chưa đủ snapshot để phân tích 7 ngày.")

        with st.expander("🧮 Vì sao dashboard cho điểm này?"):
            st.write(f"Điểm URPD cơ sở: **50/100**. Các thay đổi URPD 1D/7D được giới hạn ảnh hưởng để tránh một bucket đơn lẻ chi phối toàn bộ lớp on-chain. Điểm cuối cùng dùng **60% URPD + 40% Macro/tin tức**.")
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

# =========================
# NEWS RADAR — cập nhật độc lập với snapshot URPD
# =========================
st.markdown("---")
st.header("📰 BTC News Radar — Vĩ mô, dòng vốn và sự kiện có thể tác động BTC")
st.caption("Tin tức được làm mới khoảng mỗi 12 giờ; lịch sự kiện lọc trong 7 ngày tới. V40 hiển thị tiêu đề tiếng Việt, giữ tiêu đề tiếng Anh gốc và dịch mô tả khi có; News/Macro vẫn chiếm 40% điểm tổng hợp cùng URPD 60%.")

# V27 đã lấy News Radar trước phần báo cáo để dùng được cho điểm tổng hợp.
# Hai biến này được cache 12 giờ nên không tạo thêm lượt gọi ngoài ý muốn.

n1, n2 = st.columns([1.35, 1])
with n1:
    st.subheader("🔥 Tin mới nhất / 7 ngày qua")
    st.info("🇻🇳 Tiêu đề tiếng Việt là bản dịch để dễ đọc; 🇬🇧 tiêu đề gốc được giữ lại để đối chiếu. Nếu nguồn không có mô tả hoặc dịch vụ dịch tạm thời lỗi, dashboard sẽ giữ nguyên nội dung gốc.")
    if news_rows:
        for item in news_rows[:15]:
            impact, direction = news_impact(item["title"], item["category"])
            age_h = max(0.0, (datetime.now(timezone.utc) - item["published"]).total_seconds() / 3600.0)
            if age_h < 24:
                age = f"{age_h:.0f} giờ trước"
            else:
                age = f"{age_h/24:.1f} ngày trước"
            title_vi = news_title_vi(item)
            summary_vi = news_summary_vi(item)
            st.markdown(
                f"**🇻🇳 {title_vi}**  \n"
                f"<small>🇬🇧 <i>{item['title']}</i></small>  \n"
                f"`{item['source']}` · `{item['category']}` · `{impact}` · {direction} · `{age}`  "
                f"[Đọc tin gốc]({item['link']})",
                unsafe_allow_html=True,
            )
            if summary_vi:
                st.caption(f"📝 Tóm tắt: {summary_vi}")
            st.markdown("---")
    else:
        st.warning("Chưa lấy được nguồn tin trực tuyến. Dashboard vẫn hoạt động bình thường; thử refresh sau vài phút.")

with n2:
    st.subheader("⏰ 7 ngày sắp tới")
    if upcoming:
        for e in upcoming:
            st.markdown(
                f"**{e['date']} · {e['time']} — {e['event']}**  \n"
                f"{e['impact']} · {e['why']}"
            )
            st.markdown("---")
    else:
        st.info("Không có sự kiện trọng yếu đã cấu hình trong 7 ngày tới.")

# Bảng tóm tắt để báo cáo URPD có thêm bối cảnh vĩ mô.
st.subheader("🧭 Tác động lên BTC — đọc cùng báo cáo URPD")
if 'combined_score' in locals():
    st.info(f"**V27:** Điểm tổng hợp hiện tại **{combined_score:.0f}/100** = URPD 60% + Macro/tin tức 40%. Triển vọng: **{outlook}**.")
macro_flags = []
for item in news_rows[:20]:
    impact, direction = news_impact(item["title"], item["category"])
    if direction != "🟡 Chưa rõ":
        macro_flags.append((item, impact, direction))

if macro_flags:
    cols = st.columns(3)
    for i, (item, impact, direction) in enumerate(macro_flags[:3]):
        with cols[i % 3]:
            st.metric("Tín hiệu tin tức", direction)
            st.caption(f"{impact} · {item['category']}")
            st.write(item["title"])
else:
    st.info("Chưa có đủ tiêu đề rõ hướng để tạo tín hiệu tin tức.")

st.markdown("**Cách dashboard sẽ kết hợp:**")
st.markdown(
    "- 🟢 **URPD hỗ trợ + tin vĩ mô thuận lợi** → mức xác nhận xu hướng tăng cao hơn.  "
    "\n- 🔴 **URPD cản tăng + tin vĩ mô bất lợi** → mức xác nhận xu hướng giảm cao hơn.  "
    "\n- 🟡 **Hai bên trái chiều** → giữ trạng thái *chưa xác nhận*, không ép kết luận.  "
    "\n- ⚠️ Tin tức không được dùng để biến thành 'xác suất BTC tăng/giảm'; nó chỉ là lớp bối cảnh và catalyst cần theo dõi."
)
