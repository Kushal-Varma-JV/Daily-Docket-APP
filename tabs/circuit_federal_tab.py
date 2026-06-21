"""Federal Circuit tab display — standardized 11-field schema."""
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


def display_federal_circuit_tab():
    st.title("🔶 Federal Circuit Court Calendar")

    if st.session_state.cafc_cases:
        df = pd.DataFrame(st.session_state.cafc_cases)

        # Ensure all standard columns exist
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
            argued = len(df[df["Purpose of Hearing"] == "Oral Argument"])
            briefs = len(df[df["Purpose of Hearing"] == "On the Briefs"])
            st.metric("Argued / On Briefs", f"{argued} / {briefs}")

        st.divider()

        # ── Filters ──
        st.subheader("🔍 Filters")
        col1, col2, col3, col4 = st.columns(4)

        with col1:
            date_options = [d for d in safe_sorted_unique(df["Date"]) if d]
            cafc_date_filter = st.multiselect(
                "Date",
                options=date_options,
                default=None,
                key="cafc_date_filter",
            )
        with col2:
            nature_options = [n for n in safe_sorted_unique(df["Nature of Case"]) if n]
            cafc_nature_filter = st.multiselect(
                "Nature of Case",
                options=nature_options,
                default=None,
                key="cafc_nature_filter",
            )
        with col3:
            purpose_options = [
                p for p in safe_sorted_unique(df["Purpose of Hearing"]) if p
            ]
            cafc_purpose_filter = st.multiselect(
                "Purpose",
                options=purpose_options,
                default=None,
                key="cafc_purpose_filter",
            )
        with col4:
            cafc_search = st.text_input("Search", "", key="cafc_search")

        # ── Apply filters ──
        filtered = df.copy()

        if cafc_date_filter:
            filtered = filtered[filtered["Date"].isin(cafc_date_filter)]
        if cafc_nature_filter:
            filtered = filtered[filtered["Nature of Case"].isin(cafc_nature_filter)]
        if cafc_purpose_filter:
            filtered = filtered[filtered["Purpose of Hearing"].isin(cafc_purpose_filter)]
        if cafc_search:
            mask = filtered.apply(
                lambda r: cafc_search.lower() in str(r).lower(), axis=1
            )
            filtered = filtered[mask]

        # ── Display ──
        st.dataframe(filtered[show_cols], use_container_width=True, height=500)

        # ── Download ──
        csv = filtered[show_cols].to_csv(index=False)
        st.download_button(
            "📥 Download CSV",
            data=csv,
            file_name=f"federal_circuit_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            key="cafc_download",
        )

        # ── Raw PDF Data (debug) ──
        with st.expander("📄 View Raw PDF Data"):
            if st.session_state.cafc_raw_data:
                for idx, data in enumerate(st.session_state.cafc_raw_data, 1):
                    st.text_area(
                        f"PDF {idx} - {data.get('label', 'N/A')}",
                        value=data.get('text', '')[:5000],
                        height=300,
                        disabled=True,
                    )
            else:
                st.info("No raw PDF data available.")
    else:
        st.info("👈 Click 'Fetch Federal Circuit' in the sidebar to load data")