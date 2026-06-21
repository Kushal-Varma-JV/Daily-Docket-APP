"""DC Circuit tab display — standardized 11-field schema."""
import json
import streamlit as st
import pandas as pd
from datetime import datetime
from utils.helpers import safe_sorted_unique


def display_dc_circuit_tab():
    st.title("⚖️ DC Circuit Court Calendar")

    if st.session_state.dc_cases:
        df = pd.DataFrame(st.session_state.dc_cases)

        # --- Metrics row ---
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Total Cases", len(df))
        with col2:
            if "Date" in df.columns:
                dates = df["Date"].dropna()
                dates = dates[dates != ""]
                st.metric("Hearing Days", dates.nunique())
            else:
                st.metric("Hearing Days", "N/A")
        with col3:
            if "Judges / Panel" in df.columns:
                panels = df["Judges / Panel"].dropna()
                panels = panels[panels != ""]
                st.metric("Judge Panels", panels.nunique())
            else:
                st.metric("Judge Panels", "N/A")
        with col4:
            if "Courtroom" in df.columns:
                crs = df["Courtroom"].dropna()
                crs = crs[crs != ""]
                st.metric("Courtrooms", crs.nunique())
            else:
                st.metric("Courtrooms", "N/A")

        st.divider()

        # --- Filters ---
        st.subheader("🔍 Filters")
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            dc_date_filter = st.multiselect(
                "Filter by Date",
                options=safe_sorted_unique(df["Date"]) if "Date" in df.columns else [],
                default=None,
                key="dc_date_filter",
            )
        with col2:
            if "Judges / Panel" in df.columns:
                all_judges = [j for j in safe_sorted_unique(df["Judges / Panel"]) if j]
                dc_judge_filter = st.multiselect(
                    "Filter by Judges",
                    options=all_judges,
                    default=None,
                    key="dc_judge_filter",
                )
            else:
                dc_judge_filter = None
        with col3:
            if "Courtroom" in df.columns:
                all_crs = [c for c in safe_sorted_unique(df["Courtroom"]) if c]
                dc_cr_filter = st.multiselect(
                    "Filter by Courtroom",
                    options=all_crs,
                    default=None,
                    key="dc_cr_filter",
                )
            else:
                dc_cr_filter = None
        with col4:
            dc_search = st.text_input("Search", "", key="dc_search")

        # Apply filters
        filtered = df.copy()
        if dc_date_filter and "Date" in filtered.columns:
            filtered = filtered[filtered["Date"].isin(dc_date_filter)]
        if dc_judge_filter and "Judges / Panel" in filtered.columns:
            filtered = filtered[filtered["Judges / Panel"].isin(dc_judge_filter)]
        if dc_cr_filter and "Courtroom" in filtered.columns:
            filtered = filtered[filtered["Courtroom"].isin(dc_cr_filter)]
        if dc_search:
            mask = filtered.apply(
                lambda r: dc_search.lower() in str(r).lower(), axis=1
            )
            filtered = filtered[mask]

        # --- Data table ---
        st.subheader(f"📋 Cases ({len(filtered)} shown)")

        display_cols = [
            "Date", "Case Number", "Case Name", "Nature of Case",
            "Court Name", "Location", "Judges / Panel", "Courtroom",
            "Purpose of Hearing", "Time", "Description",
        ]
        available_cols = [c for c in display_cols if c in filtered.columns]
        st.dataframe(filtered[available_cols], use_container_width=True, height=500)

        # --- Charts ---
        st.divider()
        col1, col2, col3 = st.columns(3)
        with col1:
            st.subheader("📊 Cases by Date")
            if "Date" in filtered.columns:
                date_counts = filtered["Date"].value_counts().sort_index()
                date_counts = date_counts[date_counts.index != ""]
                if not date_counts.empty:
                    st.bar_chart(date_counts)
        with col2:
            st.subheader("📊 Cases by Courtroom")
            if "Courtroom" in filtered.columns:
                cr_counts = filtered["Courtroom"].value_counts()
                cr_counts = cr_counts[cr_counts.index != ""]
                if not cr_counts.empty:
                    st.bar_chart(cr_counts)
        with col3:
            st.subheader("📊 Cases by Panel")
            if "Judges / Panel" in filtered.columns:
                panel_counts = filtered["Judges / Panel"].value_counts()
                panel_counts = panel_counts[panel_counts.index != ""]
                if not panel_counts.empty:
                    st.bar_chart(panel_counts.head(10))

        # --- Downloads ---
        st.divider()
        col1, col2 = st.columns(2)
        with col1:
            csv = filtered.to_csv(index=False)
            st.download_button(
                "📥 Download DC Circuit as CSV",
                data=csv,
                file_name=f"dc_circuit_{datetime.now().strftime('%Y%m%d')}.csv",
                mime="text/csv",
                key="dc_download",
            )
        with col2:
            raw_json = filtered.to_dict(orient="records")
            st.download_button(
                "📥 Download Full JSON",
                data=json.dumps(raw_json, indent=2, ensure_ascii=False),
                file_name=f"dc_circuit_raw_{datetime.now().strftime('%Y%m%d')}.json",
                mime="application/json",
                key="dc_json_download",
            )

        # --- Raw HTML ---
        with st.expander("📄 View Raw HTML"):
            if st.session_state.get("dc_raw_html"):
                for idx, html in enumerate(st.session_state.dc_raw_html, 1):
                    st.text_area(
                        f"HTML Source {idx}",
                        value=html[:5000],
                        height=300,
                        disabled=True,
                        key=f"dc_raw_{idx}",
                    )
            else:
                st.info("No raw HTML data available.")

        with st.expander("🔧 View Parsed Data (JSON)"):
            raw_json = filtered.to_dict(orient="records")
            st.json(raw_json[:5])
            if len(raw_json) > 5:
                st.caption(f"Showing first 5 of {len(raw_json)} records.")
    else:
        st.info("👈 Click 'Fetch DC Circuit' in the sidebar to load data")