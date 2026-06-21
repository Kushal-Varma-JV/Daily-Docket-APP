"""Fifth Circuit tab display — standardized 11-field schema."""

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


def display_fifth_circuit_tab():
    st.title("🟡 Fifth Circuit Court Calendar")

    if not st.session_state.get("c5_cases"):
        st.info("👈 Click 'Fetch Fifth Circuit' in the sidebar to load data")
        return

    df = pd.DataFrame(st.session_state.c5_cases)

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Cases", len(df))
    with col2:
        dates = df["Date"].dropna() if "Date" in df.columns else pd.Series()
        dates = dates[dates != ""]
        st.metric("Unique Dates", dates.nunique())
    with col3:
        noc = (
            df["Nature of Case"].dropna()
            if "Nature of Case" in df.columns
            else pd.Series()
        )
        noc = noc[noc != ""]
        st.metric("Categories", noc.nunique())
    with col4:
        courtrooms = (
            df["Courtroom"].dropna()
            if "Courtroom" in df.columns
            else pd.Series()
        )
        courtrooms = courtrooms[courtrooms != ""]
        st.metric("Courtrooms", courtrooms.nunique())

    st.divider()

    # ------------------------------------------------------------------
    # Filters
    # ------------------------------------------------------------------
    st.subheader("🔍 Filters")
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        noc_filter = None
        if "Nature of Case" in df.columns:
            noc_vals = sorted(
                df["Nature of Case"]
                .dropna()[df["Nature of Case"] != ""]
                .unique()
                .tolist()
            )
            noc_filter = st.multiselect(
                "Nature of Case",
                options=noc_vals,
                default=None,
                key="c5_noc_filter",
            )

    with col2:
        date_filter = None
        if "Date" in df.columns:
            date_opts = safe_sorted_unique(df["Date"])
            date_filter = st.multiselect(
                "Date",
                options=date_opts,
                default=None,
                key="c5_date_filter",
            )

    with col3:
        cr_filter = None
        if "Courtroom" in df.columns:
            cr_vals = sorted(
                df["Courtroom"]
                .dropna()[df["Courtroom"] != ""]
                .unique()
                .tolist()
            )
            cr_filter = st.multiselect(
                "Courtroom",
                options=cr_vals,
                default=None,
                key="c5_cr_filter",
            )

    with col4:
        c5_search = st.text_input("Search", "", key="c5_search")

    # ------------------------------------------------------------------
    # Apply filters
    # ------------------------------------------------------------------
    filtered = df.copy()

    if noc_filter:
        filtered = filtered[filtered["Nature of Case"].isin(noc_filter)]
    if date_filter:
        filtered = filtered[filtered["Date"].isin(date_filter)]
    if cr_filter:
        filtered = filtered[filtered["Courtroom"].isin(cr_filter)]
    if c5_search:
        mask = filtered.apply(
            lambda r: c5_search.lower() in str(r).lower(), axis=1
        )
        filtered = filtered[mask]

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------
    show_cols = [c for c in DISPLAY_COLUMNS if c in filtered.columns]
    st.dataframe(filtered[show_cols], use_container_width=True, height=500)

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------
    csv = filtered[show_cols].to_csv(index=False)
    st.download_button(
        "📥 Download CSV",
        data=csv,
        file_name=f"fifth_circuit_{datetime.now().strftime('%Y%m%d')}.csv",
        mime="text/csv",
        key="c5_download",
    )