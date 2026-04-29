import streamlit as st
import pandas as pd
import time

from api_client import LYApiClient
from data_processor import DataProcessor
from bills_data import BILLS

# ============================================================
# 頁面設定
# ============================================================
st.set_page_config(
    page_title="立法院議事與委員發言查詢平台",
    page_icon="🏛️",
    layout="wide"
)

st.markdown("""
<style>
    .block-container { padding-top: 1.5rem; }
</style>
""", unsafe_allow_html=True)

# ============================================================
# 初始化
# ============================================================
@st.cache_resource
def get_client(_client_version="2026-04-30-id421-timeout-guard"):
    return LYApiClient()

ly_client = get_client()
processor = DataProcessor(ly_client)

if 'result_df' not in st.session_state:
    st.session_state.result_df = pd.DataFrame()
if 'current_bill' not in st.session_state:
    st.session_state.current_bill = ""

# ============================================================
# 主頁面
# ============================================================
st.title("🏛️ 立法院議事與委員發言查詢平台")
st.caption("透過 PPG 議案頁面取得審議時程，再從 IVOD 系統擷取該法案相關會議中所有委員的發言片段與純文字稿。")

# ============================================================
# 檢索區塊
# ============================================================
with st.container(border=True):
    st.subheader("🔍 查詢條件設定")

    tab_preset, tab_custom = st.tabs(["📋 預載法案清單", "🔗 自行輸入議案連結"])

    with tab_preset:
        bill_options = [f"{b['name']}（{b['session']}）" for b in BILLS]
        selected_bill_idx = st.selectbox(
            "選擇法案", range(len(bill_options)),
            format_func=lambda x: bill_options[x], key="preset_bill"
        )
        selected_bill = BILLS[selected_bill_idx]

        with st.expander("📎 查看議案連結", expanded=False):
            for u in selected_bill['urls']:
                st.markdown(f"- [{u}]({u})")

        legislator_filter = st.text_input("（選填）篩選委員姓名", placeholder="例如: 翁曉玲", key="f_preset")
        scope_col1, scope_col2 = st.columns(2)
        include_committee = scope_col1.checkbox("委員會", value=True, key="scope_committee_preset")
        include_plenary = scope_col2.checkbox("院會", value=True, key="scope_plenary_preset")
        meeting_scopes_preset = []
        if include_committee:
            meeting_scopes_preset.append("委員會")
        if include_plenary:
            meeting_scopes_preset.append("院會")
        search_preset = st.button("🚀 開始查詢", use_container_width=True, type="primary", key="btn_preset")

    with tab_custom:
        custom_url = st.text_input(
            "PPG 議案連結（多個以逗號分隔）",
            placeholder="https://ppg.ly.gov.tw/ppg/bills/603110035400000/details",
            key="custom_url"
        )
        custom_name = st.text_input("法案簡稱（選填）", placeholder="例如: 立法院職權行使法", key="custom_name")
        legislator_filter_custom = st.text_input("（選填）篩選委員姓名", placeholder="例如: 翁曉玲", key="f_custom")
        scope_col1_custom, scope_col2_custom = st.columns(2)
        include_committee_custom = scope_col1_custom.checkbox("委員會", value=True, key="scope_committee_custom")
        include_plenary_custom = scope_col2_custom.checkbox("院會", value=True, key="scope_plenary_custom")
        meeting_scopes_custom = []
        if include_committee_custom:
            meeting_scopes_custom.append("委員會")
        if include_plenary_custom:
            meeting_scopes_custom.append("院會")
        search_custom = st.button("🚀 開始查詢", use_container_width=True, type="primary", key="btn_custom")

# ============================================================
# 查詢邏輯 — 以 IVOD 爬蟲為主
# ============================================================
def run_query(bill_name, bill_urls, legislator_filter=""):
    """
    查詢流程：
    1. 解析每個 PPG 議案頁面 → 取得該法案審議過程中的 IVOD 會議連結
    2. 爬取每個 IVOD 會議頁面 → 取得所有委員發言片段
    3. 依照公報索引的會次專屬名單進行過濾
    """
    all_speeches = []
    seen_speeches = set()

    def add_speech(speech, source_url):
        speech["source_url"] = source_url
        key = (
            speech.get("speech_record_url")
            or speech.get("speech_id")
            or (
                speech.get("legislator_name", ""),
                speech.get("meeting_time", ""),
                speech.get("speech_time", ""),
                speech.get("meeting_name", ""),
            )
        )
        if key in seen_speeches:
            return False
        seen_speeches.add(key)
        all_speeches.append(speech)
        return True

    for url in bill_urls:
        with st.status(f"📄 解析議案：{url}", expanded=True) as status:
            st.write("正在解析 PPG 議案頁面...")
            bill_info = ly_client.parse_bill_page(url)
            st.write(f"✅ 法案：{bill_info['title'][:60]}...")
            st.write(f"📅 相關會議日期：{', '.join(bill_info['meeting_dates'])}")
            st.write(f"📹 IVOD 連結數量：{len(bill_info['ivod_links'])}")

            valid_speakers = set()
            gazette_queries = bill_info.get("gazette_queries", [])
            # 優先使用實際爬取到的法案名稱來萃取關鍵字
            actual_bill_name = bill_info.get('title', bill_name)
            bill_keywords = ly_client.extract_bill_keywords(actual_bill_name)

            # 建立 session_tuple → speakers 的 mapping，用於精確的會次隔離
            valid_speakers_by_session = {}
            gazette_queried_sessions = set()

            if gazette_queries:
                st.write(f"📚 找到 {len(gazette_queries)} 筆相關會次資訊，準備查詢公報發言索引 (關鍵字: {bill_keywords})...")
                for query in gazette_queries:
                    term = query["term"]
                    session = query["sessionPeriod"]
                    times = query["sessionTimes"]
                    session_tuple = (term, session, times)
                    gazette_queried_sessions.add(session_tuple)

                    st.write(f"   → 搜尋 第{term}屆 第{session}會期 第{times}次會議 公報索引...")

                    pdf_urls = ly_client.fetch_gazette_index_pdfs(term, session, times)
                    session_speakers = set()
                    for pdf_url in pdf_urls:
                        speakers = ly_client.parse_gazette_pdf_for_speakers(pdf_url, bill_keywords)
                        session_speakers.update(speakers)

                    if session_speakers:
                        st.write(f"      ✔ 找到該議程發言名單：{', '.join(session_speakers)}")
                        valid_speakers_by_session[session_tuple] = session_speakers
                        valid_speakers.update(session_speakers)
                    else:
                        st.write(f"      ⚠️ 該會次公報未明確列出此法案發言名單")

            if valid_speakers:
                st.info(f"依據公報索引，本法案議程發言名單已依照會次分離。")
            else:
                st.warning("未能從公報索引中分離出特定議程，將保留所有發言。")

            for i, ivod_link in enumerate(bill_info['ivod_links']):
                st.write(f"🔍 正在爬取 IVOD 會議 ({i+1}/{len(bill_info['ivod_links'])})...")

                ivod_result = ly_client.fetch_ivod_speech_list(ivod_link)
                # ivod_result 一定是 dict，格式: {"session_tuple": ..., "speeches": [...]}
                session_tuple = ivod_result.get("session_tuple")
                speeches = ivod_result.get("speeches", [])

                st.write(f"   → 找到 {len(speeches)} 位委員的發言片段")

                for s in speeches:
                    s['source_url'] = url
                    speaker_name = s.get('legislator_name', '')

                    # 進行過濾 (依照會次精確分離)
                    is_valid = True

                    if valid_speakers:  # 如果整體有抓到任何名單
                        if session_tuple and session_tuple in gazette_queried_sessions:
                            # 這個 IVOD 會次有在查詢列表中，使用該會次的專屬名單
                            session_speakers = valid_speakers_by_session.get(session_tuple, set())
                            if session_speakers:
                                is_valid = any(vs in speaker_name or speaker_name in vs for vs in session_speakers)
                            else:
                                # 該會次公報沒列出此法案的發言名單 (例如只是報告事項)
                                # 直接剔除該會次所有 IVOD 發言，避免顯示無關內容
                                is_valid = False
                        else:
                            # 無法確定 IVOD 會次，或該會次不在進度列表中，用全局名單做寬鬆比對
                            is_valid = any(vs in speaker_name or speaker_name in vs for vs in valid_speakers)

                    if is_valid:
                        all_speeches.append(s)

                time.sleep(0.3)

            status.update(label=f"✅ 完成 ({len(all_speeches)} 筆)", state="complete")

    if all_speeches:
        df = processor.build_dataframe_from_ivod(all_speeches, bill_short_name=bill_name)

        if legislator_filter and not df.empty:
            df = processor.filter_by_legislator(df, legislator_filter)

        return df

    return pd.DataFrame()


# 處理查詢
def run_query_api_first(bill_name, bill_urls, legislator_filter="", meeting_scopes=None):
    """
    API-first retrieval flow:
    1. Parse the PPG bill page for gazette session tuples.
    2. Parse gazette-index PDFs to identify speakers for the target agenda.
    3. Query ID421 by session tuple and speaker name to get IVOD speech URLs.
    4. Reuse the existing dataframe/export/transcript flow.
    """
    all_speeches = []
    seen_speeches = set()

    def add_speech(speech, source_url):
        speech["source_url"] = source_url
        key = (
            speech.get("speech_record_url")
            or speech.get("speech_id")
            or (
                speech.get("legislator_name", ""),
                speech.get("meeting_time", ""),
                speech.get("speech_time", ""),
                speech.get("meeting_name", ""),
            )
        )
        if key in seen_speeches:
            return False
        seen_speeches.add(key)
        all_speeches.append(speech)
        return True

    for url in bill_urls:
        with st.status(f"解析議案：{url}", expanded=True) as status:
            st.write("正在解析 PPG 議案頁面...")
            bill_info = ly_client.parse_bill_page(url)
            actual_bill_name = bill_info.get('title') or bill_name
            bill_keywords = ly_client.extract_bill_keywords(actual_bill_name)
            gazette_queries = bill_info.get("gazette_queries", [])

            st.write(f"法案：{actual_bill_name[:60]}...")
            st.write(f"屆次會期候選：{len(gazette_queries)} 筆")
            st.write(f"公報索引關鍵字：{', '.join(bill_keywords)}")

            if not gazette_queries:
                st.warning("這個法案頁沒有解析到公報屆次會期，無法進入 API-first 流程。")
                continue

            date_query_done = False
            for query in gazette_queries:
                term = query["term"]
                session = query["sessionPeriod"]
                times = query["sessionTimes"]

                if bill_info.get("meeting_dates") and not date_query_done:
                    st.write("使用法案頁的會議日期直接查 ID421 API...")
                    date_result = ly_client.fetch_speeches_by_dates(
                        term,
                        session,
                        bill_info.get("meeting_dates", []),
                        bill_keywords=bill_keywords,
                        meeting_scopes=meeting_scopes,
                        committee_names=bill_info.get("ivod_committees", []),
                    )
                    date_speeches = date_result.get("speeches", [])
                    added = 0
                    for speech in date_speeches:
                        if add_speech(speech, url):
                            added += 1
                    st.write(f"日期查詢取得 {len(date_speeches)} 筆，新增 {added} 筆。")
                    date_query_done = True

                st.write(f"搜尋第 {term} 屆第 {session} 會期第 {times} 次會議的公報索引 PDF...")
                pdf_urls = ly_client.fetch_gazette_index_pdfs(term, session, times)
                if not pdf_urls:
                    st.write("沒有找到公報索引 PDF。")
                    continue

                session_speakers = set()
                for pdf_url in pdf_urls:
                    speakers = ly_client.parse_gazette_pdf_for_speakers(pdf_url, bill_keywords)
                    session_speakers.update(speakers)

                if legislator_filter:
                    session_speakers = {
                        name for name in session_speakers
                        if legislator_filter in name or name in legislator_filter
                    }

                if not session_speakers:
                    st.write("公報索引未找到符合該議程的發言名單。")
                    continue

                st.write(f"發言名單：{', '.join(sorted(session_speakers))}")
                st.write("使用 ID421 API 依會次與委員姓名取得發言 URL...")
                api_result = ly_client.fetch_speeches_by_session_speakers(
                    term,
                    session,
                    times,
                    session_speakers,
                    meeting_dates=bill_info.get("meeting_dates", []),
                    bill_keywords=bill_keywords,
                    meeting_scopes=meeting_scopes,
                )
                speeches = api_result.get("speeches", [])
                added = 0
                for speech in speeches:
                    if add_speech(speech, url):
                        added += 1
                st.write(f"ID421 API 回傳 {len(speeches)} 筆，新增 {added} 筆。")

                time.sleep(0.2)

            status.update(label=f"完成 ({len(all_speeches)} 筆發言)", state="complete")

    if not all_speeches:
        return pd.DataFrame()

    df = processor.build_dataframe_from_ivod(all_speeches, bill_short_name=bill_name)
    if legislator_filter and not df.empty:
        df = processor.filter_by_legislator(df, legislator_filter)
    return df


if search_preset:
    if not meeting_scopes_preset:
        st.warning("請至少勾選一種會議類型：委員會或院會。")
        df = pd.DataFrame()
    else:
        df = run_query_api_first(
            selected_bill['name'],
            selected_bill['urls'],
            legislator_filter,
            meeting_scopes=meeting_scopes_preset,
        )
    st.session_state.result_df = df
    st.session_state.current_bill = selected_bill['name']

if search_custom and custom_url:
    urls = [u.strip() for u in custom_url.split(",") if u.strip()]
    name = custom_name or "自訂查詢"
    if not meeting_scopes_custom:
        st.warning("請至少勾選一種會議類型：委員會或院會。")
        df = pd.DataFrame()
    else:
        df = run_query_api_first(
            name,
            urls,
            legislator_filter_custom,
            meeting_scopes=meeting_scopes_custom,
        )
    st.session_state.result_df = df
    st.session_state.current_bill = name

# ============================================================
# 結果展示
# ============================================================
if not st.session_state.result_df.empty:
    df = st.session_state.result_df
    bill_name = st.session_state.current_bill

    st.divider()
    st.subheader(f"📋 查詢結果：{bill_name}")
    st.info(f"共找到 **{len(df)}** 筆委員發言紀錄（來源：IVOD 系統 + PPG 審議進度連結）")

    # 資料表格
    display_cols = [c for c in ['法案名稱', '會議類型', '委員姓名', '會議日期', '發言起始', '發言結束', '會議名稱'] if c in df.columns]
    st.dataframe(df[display_cols], use_container_width=True, hide_index=True)

    # ============================================================
    # 預覽與下載
    # ============================================================
    st.divider()
    st.subheader("📥 預覽與下載")

    col_preview, col_batch = st.columns([3, 2])

    with col_preview:
        st.markdown("#### 單筆預覽與下載")

        options = []
        for _, row in df.iterrows():
            label = f"{row.get('委員姓名','')} — {row.get('會議日期','')} ({row.get('發言起始','')}-{row.get('發言結束','')})"
            options.append(label)

        selected_idx = st.selectbox("選擇紀錄", range(len(options)), format_func=lambda x: options[x])

        if selected_idx is not None:
            selected_row = df.iloc[selected_idx]

            if st.button("👁️ 預覽發言內容", use_container_width=True):
                speech_url = selected_row.get('speechRecordUrl', '')
                speech_id = selected_row.get('speech_id', '')

                with st.spinner("正在擷取發言純文字..."):
                    transcript = ly_client.fetch_transcript(speech_url or speech_id)

                if transcript:
                    st.text_area("發言內容", transcript, height=350)

                    filename = processor.generate_filename(selected_row) + ".txt"
                    content = processor.export_single_txt(selected_row, transcript)
                    st.download_button(
                        label="⬇️ 下載此發言 (TXT)",
                        data=content.encode('utf-8-sig'),
                        file_name=filename,
                        mime="text/plain",
                        use_container_width=True
                    )
                else:
                    st.warning("無法取得該筆發言內容。")

    with col_batch:
        st.markdown("#### 批次下載")

        csv_data = processor.export_to_csv(df)
        safe_name = processor.sanitize_filename(bill_name, 20)
        st.download_button(
            label="📊 下載資料清單 (CSV)",
            data=csv_data,
            file_name=f"{safe_name}_發言清單.csv",
            mime="text/csv",
            use_container_width=True
        )

        st.markdown("---")

        if len(df) > 50:
            st.warning(f"共 {len(df)} 筆，批次下載需較長時間。建議先篩選委員。")

        if st.button("📦 打包所有發言 (ZIP)", use_container_width=True):
            progress_bar = st.progress(0)
            status_text = st.empty()

            def update_progress(pct):
                progress_bar.progress(pct)
                status_text.text(f"擷取與打包中... {int(pct*100)}%")

            zip_data = processor.export_to_zip(df, progress_callback=update_progress)
            progress_bar.progress(1.0)
            status_text.text("✅ 打包完成！")

            st.download_button(
                label="⬇️ 下載 ZIP",
                data=zip_data,
                file_name=f"{safe_name}_全部發言.zip",
                mime="application/zip",
                use_container_width=True
            )

elif search_preset or search_custom:
    st.warning("查無相關發言紀錄。請確認法案連結是否正確，或嘗試其他法案。")
