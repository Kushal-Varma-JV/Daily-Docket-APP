"""Ninth Circuit tab display — updated for new website + 11-field schema."""
import json
import streamlit as st
import pandas as pd
from datetime import datetime
from utils.helpers import safe_sorted_unique


def display_ninth_circuit_tab():
    st.title("🔶 Ninth Circuit Court Calendar")

    if st.session_state.c9_cases:
        df = pd.DataFrame(st.session_state.c9_cases)

        # --- Metrics row ---
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Total Cases", len(df))
        with col2:
            st.metric("Unique Dates", df['Date'].dropna().nunique()
                       if 'Date' in df.columns else 0)
        with col3:
            if 'Judges / Panel' in df.columns:
                panels = df['Judges / Panel'].dropna()
                panels = panels[panels != '']
                st.metric("Judge Panels", panels.nunique())
            else:
                st.metric("Judge Panels", "N/A")
        with col4:
            if 'Location' in df.columns:
                locs = df['Location'].dropna()
                locs = locs[locs != '']
                st.metric("Locations", locs.nunique())
            else:
                st.metric("Locations", "N/A")

        st.divider()
        st.subheader("🔍 Filters")
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            c9_date_filter = st.multiselect(
                "Filter by Date",
                options=safe_sorted_unique(df['Date']) if 'Date' in df.columns else [],
                default=None,
                key="c9_date_filter",
            )
        with col2:
            if 'Location' in df.columns:
                all_locs = [l for l in safe_sorted_unique(df['Location']) if l]
                c9_loc_filter = st.multiselect(
                    "Filter by Location",
                    options=all_locs,
                    default=None,
                    key="c9_loc_filter",
                )
            else:
                c9_loc_filter = None
        with col3:
            if 'Judges / Panel' in df.columns:
                all_judges = [j for j in safe_sorted_unique(df['Judges / Panel']) if j]
                c9_judge_filter = st.multiselect(
                    "Filter by Judges",
                    options=all_judges,
                    default=None,
                    key="c9_judge_filter",
                )
            else:
                c9_judge_filter = None
        with col4:
            c9_search = st.text_input("Search", "", key="c9_search")

        filtered = df.copy()
        if c9_date_filter and 'Date' in filtered.columns:
            filtered = filtered[filtered['Date'].isin(c9_date_filter)]
        if c9_loc_filter and 'Location' in filtered.columns:
            filtered = filtered[filtered['Location'].isin(c9_loc_filter)]
        if c9_judge_filter and 'Judges / Panel' in filtered.columns:
            filtered = filtered[filtered['Judges / Panel'].isin(c9_judge_filter)]
        if c9_search:
            mask = filtered.apply(lambda r: c9_search.lower() in str(r).lower(), axis=1)
            filtered = filtered[mask]

        st.subheader(f"📋 Cases ({len(filtered)} shown)")

        # Standard 11-field display columns
        display_cols = [
            'Date', 'Case Number', 'Case Name', 'Nature of Case',
            'Court Name', 'Location', 'Judges / Panel', 'Courtroom',
            'Purpose of Hearing', 'Time', 'Description',
        ]
        available_cols = [c for c in display_cols if c in filtered.columns]
        st.dataframe(filtered[available_cols], use_container_width=True, height=500)

        st.divider()
        col1, col2, col3 = st.columns(3)
        with col1:
            st.subheader("📊 Cases by Date")
            if 'Date' in filtered.columns:
                date_counts = filtered['Date'].value_counts().sort_index()
                date_counts = date_counts[date_counts.index != '']
                if not date_counts.empty:
                    st.bar_chart(date_counts)
        with col2:
            if 'Location' in filtered.columns:
                st.subheader("📊 Cases by Location")
                loc_counts = filtered['Location'].value_counts()
                loc_counts = loc_counts[loc_counts.index != '']
                if not loc_counts.empty:
                    st.bar_chart(loc_counts.head(10))
        with col3:
            if 'Purpose of Hearing' in filtered.columns:
                st.subheader("📊 Hearing Purpose")
                purpose_counts = filtered['Purpose of Hearing'].value_counts()
                purpose_counts = purpose_counts[purpose_counts.index != '']
                if not purpose_counts.empty:
                    st.bar_chart(purpose_counts.head(10))

        if 'Location' in filtered.columns:
            locs_present = [l for l in filtered['Location'].dropna().unique() if l]
            if locs_present:
                st.divider()
                st.subheader("🏛️ Cases by Courthouse")
                loc_cols = st.columns(min(len(locs_present), 4))
                for i, loc in enumerate(sorted(locs_present)):
                    with loc_cols[i % len(loc_cols)]:
                        loc_df = filtered[filtered['Location'] == loc]
                        short_name = loc[:40] + "…" if len(loc) > 40 else loc
                        st.metric(short_name, len(loc_df))

        st.divider()
        col1, col2 = st.columns(2)
        with col1:
            csv = filtered.to_csv(index=False)
            st.download_button(
                "📥 Download Ninth Circuit as CSV",
                data=csv,
                file_name=f"ninth_circuit_{datetime.now().strftime('%Y%m%d')}.csv",
                mime="text/csv",
                key="c9_download",
            )
        with col2:
            raw_json = filtered.to_dict(orient='records')
            st.download_button(
                "📥 Download Full JSON",
                data=json.dumps(raw_json, indent=2, ensure_ascii=False),
                file_name=f"ninth_circuit_raw_{datetime.now().strftime('%Y%m%d')}.json",
                mime="application/json",
                key="c9_json_download",
            )

        with st.expander("🔧 View Raw Data (JSON)"):
            raw_json = filtered.to_dict(orient='records')
            st.json(raw_json[:5])
            if len(raw_json) > 5:
                st.caption(f"Showing first 5 of {len(raw_json)} records.")
    else:
        st.info("👈 Click 'Fetch Ninth Circuit' in the sidebar to load data")