"""Third Circuit tab display — standardized 11-field schema."""
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


def display_third_circuit_tab():
    st.title("🟢 Third Circuit Court Calendar")

    if st.session_state.c3_cases:
        df = pd.DataFrame(st.session_state.c3_cases)

        # Ensure all columns exist
        for col in DISPLAY_COLUMNS:
            if col not in df.columns:
                df[col] = ""

        show_cols = [col for col in DISPLAY_COLUMNS if col in df.columns]

        # ── Metrics ──
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Total Cases", len(df))
        with col2:
            st.metric("Hearing Days", df["Date"].replace("", pd.NA).dropna().nunique())
        with col3:
            panels = df["Judges / Panel"].replace("", pd.NA).dropna()
            st.metric("Unique Panels", panels.nunique())
        with col4:
            if "Purpose of Hearing" in df.columns:
                argued = len(df[df["Purpose of Hearing"] == "Oral Argument"])
                submitted = len(df[df["Purpose of Hearing"] == "Submitted"])
                st.metric("Argued / Submitted", f"{argued} / {submitted}")
            else:
                st.metric("Argued / Submitted", "N/A")

        st.divider()

        # ── Filters ──
        st.subheader("🔍 Filters")
        col1, col2, col3, col4 = st.columns(4)

        with col1:
            date_opts = [d for d in safe_sorted_unique(df["Date"]) if d]
            c3_date_filter = st.multiselect(
                "Date", options=date_opts, default=None, key="c3_date_filter",
            )
        with col2:
            judge_opts = [j for j in safe_sorted_unique(df["Judges / Panel"]) if j]
            c3_judge_filter = st.multiselect(
                "Judges / Panel", options=judge_opts, default=None, key="c3_judge_filter",
            )
        with col3:
            purpose_opts = [p for p in safe_sorted_unique(df["Purpose of Hearing"]) if p]
            c3_purpose_filter = st.multiselect(
                "Type", options=purpose_opts, default=None, key="c3_purpose_filter",
            )
        with col4:
            c3_search = st.text_input("Search", "", key="c3_search")

        # ── Apply filters ──
        filtered = df.copy()
        if c3_date_filter:
            filtered = filtered[filtered["Date"].isin(c3_date_filter)]
        if c3_judge_filter:
            filtered = filtered[filtered["Judges / Panel"].isin(c3_judge_filter)]
        if c3_purpose_filter:
            filtered = filtered[filtered["Purpose of Hearing"].isin(c3_purpose_filter)]
        if c3_search:
            mask = filtered.apply(
                lambda r: c3_search.lower() in str(r).lower(), axis=1
            )
            filtered = filtered[mask]

        # ── Display ──
        st.dataframe(filtered[show_cols], use_container_width=True, height=500)

        # ── Download ──
        csv = filtered[show_cols].to_csv(index=False)
        st.download_button(
            "📥 Download CSV",
            data=csv,
            file_name=f"third_circuit_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            key="c3_download",
        )

        # ── Debug: Show rendered HTML ──
        if hasattr(st.session_state, 'c3_scraper') and hasattr(st.session_state.c3_scraper, 'get_debug_html'):
            debug_html = st.session_state.c3_scraper.get_debug_html()
            if debug_html:
                with st.expander("🔧 Debug: Rendered HTML"):
                    for key, html_snippet in debug_html.items():
                        st.text_area(
                            f"HTML: {key}",
                            value=html_snippet,
                            height=300,
                            disabled=True,
                        )
    else:
        st.info("👈 Click 'Fetch Third Circuit' in the sidebar to load data")