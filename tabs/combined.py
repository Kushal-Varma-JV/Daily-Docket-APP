"""
Combined Calendar Tab — Overhauled
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Reads directly from all 13 circuit session-state keys at render time
  (always fresh — no stale intermediary)
• ET-aware date filtering — defaults to tomorrow's cases
• 5 view modes: Tomorrow · Upcoming · Past · Custom range · All
• All circuits shown by default — user can exclude specific ones
• CSV download

Standardized 11-field schema
─────────────────────────────
  Date · Case Number · Case Name · Nature of Case · Court Name
  Location · Judges/Panel · Courtroom · Purpose of Hearing
  Time · Description
"""

import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ── Constants ────────────────────────────────────────────────────────────────
ET = ZoneInfo("America/New_York")

STANDARD_COLUMNS = [
    "Date",
    "Case Number",
    "Case Name",
    "Nature of Case",
    "Court Name",
    "Location",
    "Judges/Panel",
    "Courtroom",
    "Purpose of Hearing",
    "Time",
    "Description",
]

# ── Field-name mapping ──────────────────────────────────────────────────────
_FIELD_MAP = {
    # Date
    "date":            "Date",
    "Date":            "Date",
    "Argument Date":   "Date",
    "argument_date":   "Date",
    "hearing_date":    "Date",
    "Hearing Date":    "Date",
    # Case Number
    "case_number":     "Case Number",
    "Case Number":     "Case Number",
    "case_no":         "Case Number",
    "Case No":         "Case Number",
    "Case No.":        "Case Number",
    "Docket":          "Case Number",
    "docket":          "Case Number",
    "Docket Number":   "Case Number",
    "docket_number":   "Case Number",
    "docket_no":       "Case Number",
    # Case Name
    "case_name":       "Case Name",
    "Case Name":       "Case Name",
    "case_title":      "Case Name",
    "Case Title":      "Case Name",
    "Title":           "Case Name",
    "title":           "Case Name",
    "Style":           "Case Name",
    "style":           "Case Name",
    # Nature of Case
    "nature_of_case":  "Nature of Case",
    "Nature of Case":  "Nature of Case",
    "Nature":          "Nature of Case",
    "nature":          "Nature of Case",
    "NAC":             "Nature of Case",
    "nac":             "Nature of Case",
    # Court Name
    "court_name":      "Court Name",
    "Court Name":      "Court Name",
    "Court":           "Court Name",
    "court":           "Court Name",
    "Circuit":         "Court Name",
    "circuit":         "Court Name",
    # Location
    "location":        "Location",
    "Location":        "Location",
    "Location (City)": "Location",
    "City":            "Location",
    "city":            "Location",
    "Courthouse":      "Location",
    "courthouse":      "Location",
    # Judges/Panel
    "judges_panel":    "Judges/Panel",
    "Judges/Panel":    "Judges/Panel",
    "Judges / Panel":  "Judges/Panel",
    "judges":          "Judges/Panel",
    "Judges":          "Judges/Panel",
    "Panel":           "Judges/Panel",
    "panel":           "Judges/Panel",
    "Judge":           "Judges/Panel",
    "judge":           "Judges/Panel",
    "Judge Name, Panel": "Judges/Panel",
    # Courtroom
    "courtroom":       "Courtroom",
    "Courtroom":       "Courtroom",
    "Room":            "Courtroom",
    "room":            "Courtroom",
    "Courtroom Number":"Courtroom",
    # Purpose of Hearing
    "purpose_of_hearing":  "Purpose of Hearing",
    "Purpose of Hearing":  "Purpose of Hearing",
    "Purpose":             "Purpose of Hearing",
    "purpose":             "Purpose of Hearing",
    "Hearing Type":        "Purpose of Hearing",
    "hearing_type":        "Purpose of Hearing",
    "Hearing Purpose":     "Purpose of Hearing",
    "Type":                "Purpose of Hearing",
    # Time
    "time":            "Time",
    "Time":            "Time",
    "Argument Time":   "Time",
    "argument_time":   "Time",
    "hearing_time":    "Time",
    "Start Time":      "Time",
    # Description
    "description":     "Description",
    "Description":     "Description",
    "Notes":           "Description",
    "notes":           "Description",
    "Details":         "Description",
    "details":         "Description",
}

# Session-state key → human-readable circuit label (fallback Court Name)
_CIRCUIT_KEYS = [
    ("c1_parsed_cases", "1st Circuit"),
    ("c2_cases",        "2nd Circuit"),
    ("c3_cases",        "3rd Circuit"),
    ("c4_cases",        "4th Circuit"),
    ("c5_cases",        "5th Circuit"),
    ("c6_cases",        "6th Circuit"),
    ("c7_cases",        "7th Circuit"),
    ("c8_cases",        "8th Circuit"),
    ("c9_cases",        "9th Circuit"),
    ("c10_cases",       "10th Circuit"),
    ("c11_cases",       "11th Circuit"),
    ("dc_cases",        "DC Circuit"),
    ("cafc_cases",      "Federal Circuit"),
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalize_row(row: dict, fallback_court: str) -> dict:
    """
    Convert a single case dict (any field-naming convention) into the
    standard 11-field dict.
    """
    working = dict(row)

    # ── Circuit 4 special: split combined court/location field ──
    if "Court Name and Location" in working:
        court_and_loc = working.pop("Court Name and Location", "")
        if "—" in str(court_and_loc):
            parts = str(court_and_loc).split("—", 1)
            working.setdefault("Court Name", parts[0].strip())
            working.setdefault("Location", parts[1].strip())
        else:
            working.setdefault("Court Name", str(court_and_loc))

    # ── Map raw keys → standard keys ──
    out = {}
    used_std = set()
    for raw_key, raw_val in working.items():
        std = _FIELD_MAP.get(raw_key)
        if std and std not in used_std:
            if raw_val is None:
                val = ""
            elif isinstance(raw_val, float) and pd.isna(raw_val):
                val = ""
            else:
                val = str(raw_val).strip()
            out[std] = val
            used_std.add(std)

    # ── Ensure every standard column exists ──
    for col in STANDARD_COLUMNS:
        if col not in out:
            out[col] = ""

    # ── Fallback Court Name ──
    if out["Court Name"] == "":
        out["Court Name"] = fallback_court

    return out


def _collect_all_data() -> pd.DataFrame:
    """
    Read every circuit's session-state key at render time, normalize
    each case row, return a single DataFrame.
    """
    rows = []
    for ss_key, label in _CIRCUIT_KEYS:
        data = st.session_state.get(ss_key)
        if not data:
            continue
        for case in data:
            rows.append(_normalize_row(dict(case), fallback_court=label))

    if not rows:
        return pd.DataFrame(columns=STANDARD_COLUMNS + ["_date"])

    df = pd.DataFrame(rows, columns=STANDARD_COLUMNS)

    # Drop fully-blank rows (every field except Court Name is empty)
    check_cols = [c for c in STANDARD_COLUMNS if c != "Court Name"]
    df = df[~(df[check_cols].eq("").all(axis=1))].reset_index(drop=True)

    # Parse dates for filtering
    df["_date"] = pd.to_datetime(df["Date"], errors="coerce", dayfirst=False).dt.date

    return df


# ── Public render function ───────────────────────────────────────────────────

def display_combined_tab():
    st.title("⚖️ Combined Federal Court Calendar")

    # ── Gather data (always fresh) ───────────────────────────────────────
    combined = _collect_all_data()

    if combined.empty:
        st.info(
            "👈 No data yet. Use the sidebar to fetch data from one or more "
            "circuits, then return to this tab."
        )
        return

    # ── ET time context ──────────────────────────────────────────────────
    now_et = datetime.now(ET)
    today_et = now_et.date()
    tomorrow_et = today_et + timedelta(days=1)

    # ── Date bounds from available data ──────────────────────────────────
    valid_dates = combined["_date"].dropna()
    if valid_dates.empty:
        min_avail = today_et
        max_avail = today_et
    else:
        min_avail = valid_dates.min()
        max_avail = valid_dates.max()
    max_avail = max(max_avail, tomorrow_et)

    # ══════════════════════════════════════════════════════════════════════
    #  FILTER CONTROLS
    # ══════════════════════════════════════════════════════════════════════
    st.markdown("---")
    st.subheader("🔍 Filters")

    fcol1, fcol2, fcol3 = st.columns([1.2, 2, 1.5])

    # ── Date view mode ───────────────────────────────────────────────────
    with fcol1:
        mode = st.radio(
            "Date view",
            [
                "Tomorrow's cases",
                "Upcoming cases",
                "Past cases",
                "Custom date range",
                "All cases",
            ],
            index=0,
            key="comb_view_mode",
            help=f"Current ET: {now_et.strftime('%b %d, %Y %I:%M %p')}",
        )

    # ── Compute date window ──────────────────────────────────────────────
    filter_start = tomorrow_et
    filter_end = tomorrow_et

    if mode == "Tomorrow's cases":
        filter_start = tomorrow_et
        filter_end = tomorrow_et

    elif mode == "Upcoming cases":
        filter_start = tomorrow_et
        filter_end = max_avail

    elif mode == "Past cases":
        filter_start = min_avail
        filter_end = today_et

    elif mode == "Custom date range":
        with fcol2:
            picked = st.date_input(
                "Select date range",
                value=(min_avail, max_avail),
                min_value=min_avail,
                max_value=max_avail,
                key="comb_custom_dates",
            )
            if isinstance(picked, (list, tuple)):
                if len(picked) == 2:
                    filter_start, filter_end = picked
                elif len(picked) == 1:
                    filter_start = filter_end = picked[0]
                else:
                    filter_start, filter_end = min_avail, max_avail
            else:
                filter_start = filter_end = picked

    elif mode == "All cases":
        filter_start = min_avail
        filter_end = max_avail

    # ── Circuit filter (EXCLUDE mode — all shown by default) ─────────────
    available_circuits = sorted(
        combined["Court Name"].replace("", pd.NA).dropna().unique().tolist()
    )

    # Track whether the available circuits have changed since last render.
    # If they have (user scraped a new circuit), reset the exclude list
    # so the new circuit is automatically included.
    prev_available = st.session_state.get("_comb_prev_circuits", [])
    if sorted(prev_available) != sorted(available_circuits):
        st.session_state["_comb_prev_circuits"] = available_circuits
        # Reset the exclude widget so nothing is excluded
        st.session_state.pop("comb_circuit_exclude", None)

    with fcol3:
        excluded_circuits = st.multiselect(
            "Exclude circuits",
            options=available_circuits,
            default=[],
            key="comb_circuit_exclude",
            help="All circuits are shown by default. Select any you want to hide.",
        )

    # Build the active set (everything minus exclusions)
    active_circuits = [c for c in available_circuits if c not in excluded_circuits]

    # ── Text search ──────────────────────────────────────────────────────
    search_term = st.text_input(
        "🔎 Search Case Name or Case Number",
        "",
        key="comb_search",
    )

    # ══════════════════════════════════════════════════════════════════════
    #  APPLY FILTERS
    # ══════════════════════════════════════════════════════════════════════
    mask = combined["Court Name"].isin(active_circuits)

    # Date (skip for "All cases")
    if mode != "All cases":
        date_mask = (
            combined["_date"].notna()
            & (combined["_date"] >= filter_start)
            & (combined["_date"] <= filter_end)
        )
        mask = mask & date_mask

    # Text search
    if search_term:
        text_mask = (
            combined["Case Name"].str.contains(search_term, case=False, na=False)
            | combined["Case Number"].str.contains(search_term, case=False, na=False)
        )
        mask = mask & text_mask

    filtered = combined.loc[mask].copy()
    filtered.sort_values("_date", ascending=True, na_position="last", inplace=True)
    filtered.reset_index(drop=True, inplace=True)

    # ══════════════════════════════════════════════════════════════════════
    #  METRICS ROW
    # ══════════════════════════════════════════════════════════════════════
    st.markdown("---")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Cases", len(filtered))
    m2.metric("Circuits", filtered["Court Name"].nunique())
    m3.metric(
        "Date Range",
        f"{filter_start.strftime('%m/%d')} → {filter_end.strftime('%m/%d')}"
        if mode != "All cases" else "All",
    )
    m4.metric("🕐 ET Now", now_et.strftime("%I:%M %p"))

    # Date-range caption
    if mode not in ("All cases", "Custom date range"):
        st.caption(
            f"Showing **{filter_start.strftime('%b %d, %Y')}** → "
            f"**{filter_end.strftime('%b %d, %Y')}**"
        )

    # ══════════════════════════════════════════════════════════════════════
    #  DATA TABLE
    # ══════════════════════════════════════════════════════════════════════
    st.subheader("📋 Case Details")

    if filtered.empty:
        if mode == "Tomorrow's cases":
            st.warning(
                f"No cases scheduled for tomorrow "
                f"({tomorrow_et.strftime('%b %d, %Y')}). "
                "Try **Upcoming cases** or **All cases**."
            )
        else:
            st.warning(
                "No cases match the current filters. "
                "Try adjusting the date range or circuits."
            )
    else:
        display_df = filtered.drop(columns=["_date"])
        st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True,
            height=min(len(display_df) * 38 + 50, 800),
        )

        # Download
        csv = display_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="📥 Download filtered calendar as CSV",
            data=csv,
            file_name=f"daily_docket_combined_{today_et.isoformat()}.csv",
            mime="text/csv",
            key="comb_download",
        )
