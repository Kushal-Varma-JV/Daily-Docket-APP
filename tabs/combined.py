"""Combined Calendar tab display — standardised 10-field layout."""

import streamlit as st
import pandas as pd
from datetime import datetime
from utils.helpers import safe_sorted_unique

# ── Must match app.py STANDARD_COLUMNS ──────────────────────────────────
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

# Short labels for the metrics row (extracted from Court Name)
SHORT_LABELS = {
    "United States Court of Appeals for the First Circuit":    "1st",
    "United States Court of Appeals for the Second Circuit":   "2nd",
    "United States Court of Appeals for the Third Circuit":    "3rd",
    "United States Court of Appeals for the Fourth Circuit":   "4th",
    "United States Court of Appeals for the Fifth Circuit":    "5th",
    "United States Court of Appeals for the Sixth Circuit":    "6th",
    "United States Court of Appeals for the Seventh Circuit":  "7th",
    "United States Court of Appeals for the Eighth Circuit":   "8th",
    "United States Court of Appeals for the Ninth Circuit":    "9th",
    "United States Court of Appeals for the Tenth Circuit":    "10th",
    "United States Court of Appeals for the Eleventh Circuit": "11th",
    "United States Court of Appeals for the District of Columbia Circuit": "DC",
    "United States Court of Appeals for the Federal Circuit":  "Fed",
}


def display_combined_tab():
    st.title("⚖️ Combined Federal Court Calendar")

    if st.session_state.combined_cases:
        df = pd.DataFrame(st.session_state.combined_cases)

        # ── Guarantee column order & presence ───────────────────────────
        for col in STANDARD_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        df = df[STANDARD_COLUMNS]

        # ── Top-row metrics ─────────────────────────────────────────────
        active_courts = (
            df["Court Name"]
            .replace("", pd.NA)
            .dropna()
            .unique()
            .tolist()
        )
        # Sort by the short label order
        label_order = list(SHORT_LABELS.keys())
        active_courts = sorted(
            active_courts,
            key=lambda x: label_order.index(x) if x in label_order else 999,
        )

        num_cols = 1 + len(active_courts) + 1
        cols = st.columns(num_cols)

        with cols[0]:
            st.metric("Total Cases", len(df))

        for i, court in enumerate(active_courts):
            with cols[i + 1]:
                short = SHORT_LABELS.get(court, court[:10])
                st.metric(short, len(df[df["Court Name"] == court]))

        with cols[-1]:
            st.metric("Unique Dates", df["Date"].replace("", pd.NA).dropna().nunique())

        st.divider()

        # ── Filters ─────────────────────────────────────────────────────
        st.subheader("🔍 Filters")
        col1, col2, col3 = st.columns(3)

        with col1:
            court_options = safe_sorted_unique(df["Court Name"])
            court_filter = st.multiselect(
                "Filter by Court",
                options=court_options,
                default=court_options,
                key="combined_court_filter",
            )

        with col2:
            date_options = safe_sorted_unique(df["Date"])
            date_filter = st.multiselect(
                "Filter by Date",
                options=date_options,
                default=None,
                key="combined_date_filter",
            )

        with col3:
            search_term = st.text_input(
                "Search Case Name / Number", "", key="combined_search"
            )

        # ── Apply filters ───────────────────────────────────────────────
        filtered_df = df.copy()

        if court_filter:
            filtered_df = filtered_df[filtered_df["Court Name"].isin(court_filter)]

        if date_filter:
            filtered_df = filtered_df[filtered_df["Date"].isin(date_filter)]

        if search_term:
            mask = pd.Series(False, index=filtered_df.index)
            mask = mask | filtered_df["Case Name"].str.contains(
                search_term, case=False, na=False
            )
            mask = mask | filtered_df["Case Number"].str.contains(
                search_term, case=False, na=False
            )
            filtered_df = filtered_df[mask]

        # ── Display ─────────────────────────────────────────────────────
        st.subheader("📋 Case Details")
        st.dataframe(filtered_df, use_container_width=True, height=500)

        csv = filtered_df.to_csv(index=False)
        st.download_button(
            "📥 Download as CSV",
            data=csv,
            file_name=f"combined_calendar_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
        )
    else:
        st.info("👈 Fetch data from one or more circuits using the sidebar")
