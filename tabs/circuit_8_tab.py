"""Eighth Circuit tab display — standardized 11-field schema."""
import streamlit as st
import pandas as pd
from datetime import datetime
from utils.helpers import safe_sorted_unique


DISPLAY_COLUMNS = [
    "Date",
    "Case Number",
    "Case Name",
    "Nature of Case",
    "Court Name",
    "Location",
    "Judges / Panel",
    "Courtroom",
    "Purpose of Hearing",
    "Time",
    "Description",
]


def display_eighth_circuit_tab():
    st.title("🟤 Eighth Circuit Court Calendar")
    if st.session_state.c8_cases:
        df = pd.DataFrame(st.session_state.c8_cases)
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Total Cases", len(df))
        with col2:
            st.metric("Unique Dates", df['Date'].dropna().nunique())
        with col3:
            panels = df['Judges / Panel'].replace("", pd.NA).dropna() if 'Judges / Panel' in df.columns else pd.Series(dtype=str)
            st.metric("Judge Panels", panels.nunique())
        with col4:
            courtrooms = df['Courtroom'].replace("", pd.NA).dropna() if 'Courtroom' in df.columns else pd.Series(dtype=str)
            st.metric("Courtrooms", courtrooms.nunique())
        st.divider()
        st.subheader("🔍 Filters")
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            c8_date_filter = st.multiselect("Date", options=safe_sorted_unique(df['Date']),
                                            default=None, key="c8_date_filter")
        with col2:
            all_judges = ([j for j in safe_sorted_unique(df['Judges / Panel']) if j]
                          if 'Judges / Panel' in df.columns else [])
            c8_judge_filter = st.multiselect("Judges / Panel", options=all_judges,
                                             default=None, key="c8_judge_filter")
        with col3:
            all_courtrooms = ([c for c in safe_sorted_unique(df['Courtroom']) if c]
                              if 'Courtroom' in df.columns else [])
            c8_courtroom_filter = st.multiselect("Courtroom", options=all_courtrooms,
                                                 default=None, key="c8_courtroom_filter")
        with col4:
            c8_search = st.text_input("Search", "", key="c8_search")
        filtered = df.copy()
        if c8_date_filter:
            filtered = filtered[filtered['Date'].isin(c8_date_filter)]
        if c8_judge_filter:
            filtered = filtered[filtered['Judges / Panel'].isin(c8_judge_filter)]
        if c8_courtroom_filter:
            filtered = filtered[filtered['Courtroom'].isin(c8_courtroom_filter)]
        if c8_search:
            mask = filtered.apply(lambda r: c8_search.lower() in str(r).lower(), axis=1)
            filtered = filtered[mask]
        show_cols = [c for c in DISPLAY_COLUMNS if c in filtered.columns]
        st.dataframe(filtered[show_cols], use_container_width=True, height=500)
        st.divider()
        st.subheader("📊 Cases by Date")
        date_counts = filtered['Date'].value_counts().sort_index()
        date_counts = date_counts[date_counts.index != '']
        if not date_counts.empty:
            st.bar_chart(date_counts)
        csv = filtered[show_cols].to_csv(index=False)
        st.download_button("📥 Download CSV", data=csv,
                           file_name=f"eighth_circuit_{datetime.now().strftime('%Y%m%d')}.csv",
                           mime="text/csv", key="c8_download")
        if st.session_state.c8_raw_texts:
            with st.expander("📄 Raw PDF Text"):
                for pdf_name, raw_text in st.session_state.c8_raw_texts.items():
                    st.text_area(f"Raw text — {pdf_name}", value=raw_text, height=300,
                                 disabled=True, key=f"c8_raw_{pdf_name}")
    else:
        st.info("👈 Click 'Fetch Eighth Circuit' in the sidebar to load data")