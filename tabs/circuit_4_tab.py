"""Fourth Circuit tab display."""
import re
import streamlit as st
import pandas as pd
from datetime import datetime
from utils.helpers import safe_sorted_unique


DISPLAY_COLUMNS = [
    'Case Name',
    'Case Number',
    'Nature of Case',
    'Court Name and Location',
    'Judge Name, Panel',
    'Courtroom Number',
    'Purpose of Hearing',
    'Description',
    'Date',
]


def display_fourth_circuit_tab():
    st.title("🟠 Fourth Circuit Court Calendar")

    if not st.session_state.get("c4_cases"):
        st.info("👈 Click 'Fetch Fourth Circuit' in the sidebar to load data")
        return

    df = pd.DataFrame(st.session_state.c4_cases)

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Cases", len(df))
    with col2:
        st.metric("Unique Dates", df['Date'].dropna().nunique() if 'Date' in df.columns else 0)
    with col3:
        if 'Nature of Case' in df.columns:
            noc = df['Nature of Case'].dropna()
            noc = noc[noc != '']
            st.metric("Categories", noc.nunique())
        else:
            st.metric("Categories", 0)
    with col4:
        if 'Judge Name, Panel' in df.columns:
            panels = df['Judge Name, Panel'].dropna()
            panels = panels[panels != '']
            st.metric("Panels Assigned", len(panels))
        else:
            st.metric("Panels Assigned", 0)

    st.divider()

    # ------------------------------------------------------------------
    # Filters
    # ------------------------------------------------------------------
    st.subheader("🔍 Filters")
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        if 'Nature of Case' in df.columns:
            noc_vals = df['Nature of Case'].dropna()
            noc_vals = sorted(noc_vals[noc_vals != ''].unique().tolist())
            noc_filter = st.multiselect(
                "Nature of Case", options=noc_vals, default=None,
                key="c4_noc_filter",
            )
        else:
            noc_filter = None

    with col2:
        if 'Date' in df.columns:
            date_opts = safe_sorted_unique(df['Date'])
            date_filter = st.multiselect(
                "Date", options=date_opts, default=None,
                key="c4_date_filter",
            )
        else:
            date_filter = None

    with col3:
        if 'Judge Name, Panel' in df.columns:
            panel_vals = df['Judge Name, Panel'].dropna()
            panel_vals = sorted(panel_vals[panel_vals != ''].unique().tolist())
            panel_filter = st.multiselect(
                "Judge / Panel", options=panel_vals, default=None,
                key="c4_panel_filter",
            )
        else:
            panel_filter = None

    with col4:
        c4_search = st.text_input("Search", "", key="c4_search")

    # ------------------------------------------------------------------
    # Apply filters
    # ------------------------------------------------------------------
    filtered = df.copy()

    if noc_filter:
        filtered = filtered[filtered['Nature of Case'].isin(noc_filter)]

    if date_filter:
        filtered = filtered[filtered['Date'].isin(date_filter)]

    if panel_filter:
        filtered = filtered[filtered['Judge Name, Panel'].isin(panel_filter)]

    if c4_search:
        mask = filtered.apply(lambda r: c4_search.lower() in str(r).lower(), axis=1)
        filtered = filtered[mask]

    # ------------------------------------------------------------------
    # Display — only show columns that exist
    # ------------------------------------------------------------------
    show_cols = [c for c in DISPLAY_COLUMNS if c in filtered.columns]
    st.dataframe(filtered[show_cols], use_container_width=True, height=500)

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------
    csv = filtered[show_cols].to_csv(index=False)
    st.download_button(
        "📥 Download CSV", data=csv,
        file_name=f"fourth_circuit_{datetime.now().strftime('%Y%m%d')}.csv",
        mime="text/csv", key="c4_download",
    )