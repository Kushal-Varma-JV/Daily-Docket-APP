"""Ninth Circuit tab display — updated for snake_case 11-field schema."""
import json
import streamlit as st
import pandas as pd
from datetime import datetime
from utils.helpers import safe_sorted_unique


# ── Column mapping: snake_case scraper keys → display names ──────────────
_DISPLAY_MAP = {
    "date":               "Date",
    "case_number":        "Case Number",
    "case_name":          "Case Name",
    "nature_of_case":     "Nature of Case",
    "court_name":         "Court Name",
    "location":           "Location",
    "judges_panel":       "Judges/Panel",
    "courtroom":          "Courtroom",
    "purpose_of_hearing": "Purpose of Hearing",
    "time":               "Time",
    "description":        "Description",
}

_DISPLAY_ORDER = list(_DISPLAY_MAP.values())


def _rename_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename snake_case columns to display-friendly Title Case."""
    return df.rename(columns=_DISPLAY_MAP)


def display_ninth_circuit_tab():
    st.title("🔶 Ninth Circuit Court Calendar")

    if not st.session_state.c9_cases:
        st.info("👈 Click 'Fetch Ninth Circuit' in the sidebar to load data")
        return

    # ── Build DataFrame and rename to display names ──────────────────
    raw_df = pd.DataFrame(st.session_state.c9_cases)
    df = _rename_columns(raw_df)

    # ── Metrics row ──────────────────────────────────────────────────
    m1, m2, m3, m4, m5 = st.columns(5)
    with m1:
        st.metric("Total Cases", len(df))
    with m2:
        st.metric(
            "Unique Dates",
            df["Date"].dropna().nunique() if "Date" in df.columns else 0,
        )
    with m3:
        if "Judges/Panel" in df.columns:
            panels = df["Judges/Panel"].dropna()
            panels = panels[panels != ""]
            st.metric("Judge Panels", panels.nunique())
        else:
            st.metric("Judge Panels", "N/A")
    with m4:
        if "Location" in df.columns:
            locs = df["Location"].dropna()
            locs = locs[locs != ""]
            st.metric("Locations", locs.nunique())
        else:
            st.metric("Locations", "N/A")
    with m5:
        if "Description" in df.columns:
            with_desc = df["Description"].dropna()
            with_desc = with_desc[with_desc != ""]
            st.metric("With Synopsis", len(with_desc))
        else:
            st.metric("With Synopsis", "N/A")

    # ── Filters ──────────────────────────────────────────────────────
    st.divider()
    st.subheader("🔍 Filters")
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        c9_date_filter = st.multiselect(
            "Filter by Date",
            options=safe_sorted_unique(df["Date"]) if "Date" in df.columns else [],
            default=None,
            key="c9_date_filter",
        )

    with col2:
        if "Location" in df.columns:
            all_locs = [loc for loc in safe_sorted_unique(df["Location"]) if loc]
            c9_loc_filter = st.multiselect(
                "Filter by Location",
                options=all_locs,
                default=None,
                key="c9_loc_filter",
            )
        else:
            c9_loc_filter = None

    with col3:
        if "Judges/Panel" in df.columns:
            all_judges = [j for j in safe_sorted_unique(df["Judges/Panel"]) if j]
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

    # ── Apply filters ────────────────────────────────────────────────
    filtered = df.copy()

    if c9_date_filter and "Date" in filtered.columns:
        filtered = filtered[filtered["Date"].isin(c9_date_filter)]

    if c9_loc_filter and "Location" in filtered.columns:
        filtered = filtered[filtered["Location"].isin(c9_loc_filter)]

    if c9_judge_filter and "Judges/Panel" in filtered.columns:
        filtered = filtered[filtered["Judges/Panel"].isin(c9_judge_filter)]

    if c9_search:
        mask = filtered.apply(
            lambda r: c9_search.lower() in str(r).lower(), axis=1
        )
        filtered = filtered[mask]

    # ── Data table ───────────────────────────────────────────────────
    st.subheader(f"📋 Cases ({len(filtered)} shown)")

    available_cols = [c for c in _DISPLAY_ORDER if c in filtered.columns]
    st.dataframe(
        filtered[available_cols],
        use_container_width=True,
        hide_index=True,
        height=min(len(filtered) * 38 + 50, 600),
    )

    # ── Charts ───────────────────────────────────────────────────────
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
        if "Location" in filtered.columns:
            st.subheader("📊 Cases by Location")
            loc_counts = filtered["Location"].value_counts()
            loc_counts = loc_counts[loc_counts.index != ""]
            if not loc_counts.empty:
                st.bar_chart(loc_counts.head(10))

    with col3:
        if "Purpose of Hearing" in filtered.columns:
            st.subheader("📊 Hearing Purpose")
            purpose_counts = filtered["Purpose of Hearing"].value_counts()
            purpose_counts = purpose_counts[purpose_counts.index != ""]
            if not purpose_counts.empty:
                st.bar_chart(purpose_counts.head(10))

    # ── Courthouse breakdown ─────────────────────────────────────────
    if "Location" in filtered.columns:
        locs_present = [
            loc for loc in filtered["Location"].dropna().unique() if loc
        ]
        if locs_present:
            st.divider()
            st.subheader("🏛️ Cases by Courthouse")
            loc_cols = st.columns(min(len(locs_present), 4))
            for i, loc in enumerate(sorted(locs_present)):
                with loc_cols[i % len(loc_cols)]:
                    loc_df = filtered[filtered["Location"] == loc]
                    short_name = (
                        loc[:40] + "…" if len(loc) > 40 else loc
                    )
                    st.metric(short_name, len(loc_df))

    # ── Synopsis preview section ─────────────────────────────────────
    if "Description" in filtered.columns:
        cases_with_synopsis = filtered[
            filtered["Description"].fillna("").str.strip() != ""
        ]
        if not cases_with_synopsis.empty:
            st.divider()
            st.subheader(
                f"📝 Case Synopses ({len(cases_with_synopsis)} available)"
            )
            for _, row in cases_with_synopsis.head(10).iterrows():
                with st.expander(
                    f"**{row.get('Case Number', '')}** — "
                    f"{row.get('Case Name', 'Unknown')}"
                ):
                    synopsis = row.get("Description", "")
                    st.write(synopsis)
                    detail_cols = st.columns(4)
                    with detail_cols[0]:
                        st.caption(f"📅 {row.get('Date', '')}")
                    with detail_cols[1]:
                        st.caption(f"📍 {row.get('Location', '')}")
                    with detail_cols[2]:
                        st.caption(
                            f"⚖️ {row.get('Judges/Panel', 'TBD')}"
                        )
                    with detail_cols[3]:
                        st.caption(f"🕐 {row.get('Time', '')}")

            if len(cases_with_synopsis) > 10:
                st.caption(
                    f"Showing first 10 of "
                    f"{len(cases_with_synopsis)} synopses. "
                    f"Download CSV for all."
                )

    # ── Downloads ────────────────────────────────────────────────────
    st.divider()
    col1, col2 = st.columns(2)

    with col1:
        csv = filtered.to_csv(index=False)
        st.download_button(
            "📥 Download Ninth Circuit as CSV",
            data=csv,
            file_name=(
                f"ninth_circuit_"
                f"{datetime.now().strftime('%Y%m%d')}.csv"
            ),
            mime="text/csv",
            key="c9_download",
        )

    with col2:
        raw_json = filtered.to_dict(orient="records")
        st.download_button(
            "📥 Download Full JSON",
            data=json.dumps(raw_json, indent=2, ensure_ascii=False),
            file_name=(
                f"ninth_circuit_raw_"
                f"{datetime.now().strftime('%Y%m%d')}.json"
            ),
            mime="application/json",
            key="c9_json_download",
        )

    with st.expander("🔧 View Raw Data (JSON)"):
        raw_json = filtered.to_dict(orient="records")
        st.json(raw_json[:5])
        if len(raw_json) > 5:
            st.caption(
                f"Showing first 5 of {len(raw_json)} records."
            )
