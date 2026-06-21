"""Seventh Circuit tab display — standardized 11-field schema."""
import streamlit as st
import pandas as pd
from datetime import datetime
from utils.helpers import safe_sorted_unique


def display_seventh_circuit_tab():
    st.title("⚪ Seventh Circuit Court Calendar")
    if st.session_state.c7_cases:
        df = pd.DataFrame(st.session_state.c7_cases)
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Total Cases", len(df))
        with col2:
            st.metric("Unique Dates", df['Date'].dropna().nunique())
        with col3:
            case_nums = df['Case Number'].dropna()
            case_nums = case_nums[case_nums != '']
            st.metric("Case Numbers", case_nums.nunique())
        with col4:
            judges = df['Judges / Panel'].replace("", pd.NA).dropna() if 'Judges / Panel' in df.columns else pd.Series(dtype=str)
            st.metric("Judge Panels", judges.nunique())
        st.divider()
        st.subheader("🔍 Filters")
        col1, col2, col3 = st.columns(3)
        with col1:
            c7_date_filter = st.multiselect("Date", options=safe_sorted_unique(df['Date']),
                                            default=None, key="c7_date_filter")
        with col2:
            all_judges = ([j for j in safe_sorted_unique(df['Judges / Panel']) if j]
                          if 'Judges / Panel' in df.columns else [])
            c7_judge_filter = st.multiselect("Judges / Panel", options=all_judges,
                                             default=None, key="c7_judge_filter")
        with col3:
            c7_search = st.text_input("Search", "", key="c7_search")
        filtered = df.copy()
        if c7_date_filter:
            filtered = filtered[filtered['Date'].isin(c7_date_filter)]
        if c7_judge_filter:
            filtered = filtered[filtered['Judges / Panel'].isin(c7_judge_filter)]
        if c7_search:
            mask = filtered.apply(lambda r: c7_search.lower() in str(r).lower(), axis=1)
            filtered = filtered[mask]
        st.dataframe(filtered, use_container_width=True, height=500)
        csv = filtered.to_csv(index=False)
        st.download_button("📥 Download CSV", data=csv,
                           file_name=f"seventh_circuit_{datetime.now().strftime('%Y%m%d')}.csv",
                           mime="text/csv", key="c7_download")
        if st.session_state.c7_raw_texts:
            with st.expander("📄 Raw PDF Text"):
                for pdf_name, raw_text in st.session_state.c7_raw_texts.items():
                    st.text_area(f"Raw text — {pdf_name}", value=raw_text, height=300,
                                 disabled=True, key=f"c7_raw_{pdf_name}")
    else:
        st.info("👈 Click 'Fetch Seventh Circuit' in the sidebar to load data")