"""Second Circuit tab display — standardised 10-field layout."""
import streamlit as st
import pandas as pd

# ── Must match the scraper's STANDARD_COLUMNS exactly ──────────────────
STANDARD_COLUMNS = [
    "Case Name",
    "Case Number",
    "Nature of Case",
    "Court Name",
    "Location (City)",
    "Judges / Panel",
    "Courtroom",
    "Purpose of Hearing",
    "Date",
    "Time",
]


def display_second_circuit_tab():
    st.title("🔴 Second Circuit Court Calendar")

    if st.session_state.c2_cases:
        df = pd.DataFrame(st.session_state.c2_cases)

        # ── Guarantee column order & presence ───────────────────────────
        for col in STANDARD_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        df = df[STANDARD_COLUMNS]

        # ── Metrics row ─────────────────────────────────────────────────
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Total Cases", len(df))
        with col2:
            st.metric("Hearing Days", df["Date"].dropna().nunique())
        with col3:
            st.metric("Locations", df["Location (City)"].replace("", pd.NA).dropna().nunique())
        with col4:
            judges_series = df["Judges / Panel"].replace("", pd.NA).dropna()
            st.metric("Panels", judges_series.nunique())

        st.divider()

        # ── Filters ─────────────────────────────────────────────────────
        filter_col1, filter_col2, filter_col3 = st.columns(3)

        with filter_col1:
            dates = sorted(df["Date"].dropna().unique())
            selected_date = st.selectbox(
                "Filter by Date", ["All"] + list(dates), key="c2_date_filter"
            )

        with filter_col2:
            locations = sorted(
                df["Location (City)"].replace("", pd.NA).dropna().unique()
            )
            selected_location = st.selectbox(
                "Filter by Location", ["All"] + list(locations), key="c2_loc_filter"
            )

        with filter_col3:
            judges = sorted(
                df["Judges / Panel"].replace("", pd.NA).dropna().unique()
            )
            selected_judge = st.selectbox(
                "Filter by Panel", ["All"] + list(judges), key="c2_judge_filter"
            )

        # ── Apply filters ───────────────────────────────────────────────
        filtered = df.copy()
        if selected_date != "All":
            filtered = filtered[filtered["Date"] == selected_date]
        if selected_location != "All":
            filtered = filtered[filtered["Location (City)"] == selected_location]
        if selected_judge != "All":
            filtered = filtered[filtered["Judges / Panel"] == selected_judge]

        st.dataframe(filtered, use_container_width=True, height=500)

    else:
        st.info("👈 Click 'Fetch Second Circuit' in the sidebar to load data")