"""Sixth Circuit tab display — standardized 11-field schema."""
import streamlit as st
import pandas as pd
from datetime import datetime
from utils.helpers import safe_sorted_unique


def display_sixth_circuit_tab():
    st.title("🟣 Sixth Circuit Court Calendar")
    if st.session_state.c6_cases:
        df = pd.DataFrame(st.session_state.c6_cases)
        col1, col2, col3, col4, col5 = st.columns(5)
        with col1:
            st.metric("Total Cases", len(df))
        with col2:
            st.metric("Unique Dates", df['Date'].dropna().nunique())
        with col3:
            st.metric("Judge Panels",
                       df['Judges / Panel'].replace("", pd.NA).dropna().nunique() if 'Judges / Panel' in df.columns else "N/A")
        with col4:
            types = df['Purpose of Hearing'].dropna() if 'Purpose of Hearing' in df.columns else pd.Series(dtype=str)
            types = types[types != '']
            st.metric("Hearing Types", types.nunique())
        with col5:
            courts = df['Court Name'].dropna() if 'Court Name' in df.columns else pd.Series(dtype=str)
            courts = courts[courts != '']
            st.metric("Courts", courts.nunique())
        st.divider()
        st.subheader("🔍 Filters")
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            c6_date_filter = st.multiselect("Date", options=safe_sorted_unique(df['Date']),
                                            default=None, key="c6_date_filter")
        with col2:
            all_judges = ([j for j in safe_sorted_unique(df['Judges / Panel']) if j]
                          if 'Judges / Panel' in df.columns else [])
            c6_judge_filter = st.multiselect("Panel", options=all_judges, default=None,
                                             key="c6_judge_filter")
        with col3:
            all_courts = ([c for c in safe_sorted_unique(df['Court Name']) if c]
                          if 'Court Name' in df.columns else [])
            c6_court_filter = st.multiselect("Court Name", options=all_courts,
                                             default=None, key="c6_court_filter")
        with col4:
            c6_search = st.text_input("Search", "", key="c6_search")
        filtered = df.copy()
        if c6_date_filter:
            filtered = filtered[filtered['Date'].isin(c6_date_filter)]
        if c6_judge_filter:
            filtered = filtered[filtered['Judges / Panel'].isin(c6_judge_filter)]
        if c6_court_filter:
            filtered = filtered[filtered['Court Name'].isin(c6_court_filter)]
        if c6_search:
            mask = filtered.apply(lambda r: c6_search.lower() in str(r).lower(), axis=1)
            filtered = filtered[mask]
        st.dataframe(filtered, use_container_width=True, height=500)
        csv = filtered.to_csv(index=False)
        st.download_button("📥 Download CSV", data=csv,
                           file_name=f"sixth_circuit_{datetime.now().strftime('%Y%m%d')}.csv",
                           mime="text/csv", key="c6_download")
    else:
        st.info("👈 Click 'Fetch Sixth Circuit' in the sidebar to load data")