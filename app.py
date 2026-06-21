"""
Federal Court Calendar Analyzer — All 13 Circuits
Streamlit app entry point. All scraping logic lives in scrapers/,
all tab displays in tabs/, shared utilities in utils/.
"""

import time
import streamlit as st
import pandas as pd
from datetime import datetime

# ── Utils ──
from utils.constants import (
    DEFAULT_C1_URL, DEFAULT_C8_URL,
)
from utils.helpers import HAS_PDFPLUMBER, HAS_PYPDF2, HAS_PLAYWRIGHT

# ── Scrapers ──
from scrapers.circuit_1 import fetch_pdf_bytes, extract_text_from_pdf_bytes, parse_calendar_data
from scrapers.circuit_2 import USCA2Scraper
from scrapers.circuit_3 import CA3CourtScraper, run_c3_scraper_in_thread
from scrapers.circuit_4 import USCA4Scraper
from scrapers.circuit_5 import USCA5Scraper
from scrapers.circuit_6 import USCA6Scraper
from scrapers.circuit_7 import USCA7Scraper
from scrapers.circuit_8 import USCA8Scraper
from scrapers.circuit_9 import USCA9Scraper
from scrapers.circuit_10 import USCA10Scraper
from scrapers.circuit_11 import USCA11Scraper
from scrapers.circuit_dc import USCADCScraper
from scrapers.circuit_federal import USCAFCScraper

# ── Tab displays ──
from tabs.combined import display_combined_tab
from tabs.circuit_1_tab import display_first_circuit_tab
from tabs.circuit_2_tab import display_second_circuit_tab
from tabs.circuit_3_tab import display_third_circuit_tab
from tabs.circuit_4_tab import display_fourth_circuit_tab
from tabs.circuit_5_tab import display_fifth_circuit_tab
from tabs.circuit_6_tab import display_sixth_circuit_tab
from tabs.circuit_7_tab import display_seventh_circuit_tab
from tabs.circuit_8_tab import display_eighth_circuit_tab
from tabs.circuit_9_tab import display_ninth_circuit_tab
from tabs.circuit_10_tab import display_tenth_circuit_tab
from tabs.circuit_11_tab import display_eleventh_circuit_tab
from tabs.circuit_dc_tab import display_dc_circuit_tab
from tabs.circuit_federal_tab import display_federal_circuit_tab


# ── PDF library check ──
if not HAS_PDFPLUMBER and not HAS_PYPDF2:
    st.error(
        "Need at least one PDF library.\n"
        "pip install pdfplumber  (recommended)\n"
        "  or\n"
        "pip install PyPDF2"
    )
    st.stop()


# ══════════════════════════════════════════════════════════════════════════
#  STANDARD 10-FIELD SCHEMA  (single source of truth for the whole app)
# ══════════════════════════════════════════════════════════════════════════
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


def _normalize_to_standard(cases: list, court_name_fallback: str) -> list:
    """
    Bridge function: converts ANY scraper output (old-style or new-style)
    into the standard 10-field schema.

    Handles five formats:

      New 10-field (Circuits 1 & 2, already converted):
          Case Name, Case Number, Nature of Case, Court Name,
          Location (City), Judges / Panel, Courtroom,
          Purpose of Hearing, Date, Time

      Circuit 5 style (new 10-field with "Location" instead of "Location (City)"
      and "Judges/Panel" without spaces around the slash):
          Case Name, Case Number, Nature of Case, Court Name,
          Location, Judges/Panel, Courtroom,
          Purpose of Hearing, Date, Time

      New 11-field style (Circuits 9, 10 — "Location" and "Judges / Panel"
      with spaces, plus optional "Description"):
          Case Name, Case Number, Nature of Case, Court Name,
          Location, Judges / Panel, Courtroom,
          Purpose of Hearing, Date, Time, Description

      Circuit 4 style (9-field with combined court/location):
          Case Name, Case Number, Nature of Case,
          Court Name and Location, Judge Name, Panel,
          Courtroom Number, Purpose of Hearing, Description, Date

      Old-style (remaining circuits, not yet converted):
          Circuit, Date, Time, Case Number, Case Name, Judges,
          Location, Courtroom, Time Allotted, Session Type, etc.

    Once ALL scrapers are converted, this function collapses to a no-op
    and can be removed.
    """
    out = []
    for c in cases:
        row = dict(c)  # shallow copy

        # ── Detect format ──
        is_new_10 = "Court Name" in row and "Location (City)" in row
        is_c5_style = (
            "Court Name" in row
            and "Location" in row
            and "Location (City)" not in row
            and "Court Name and Location" not in row
            and "Judges/Panel" in row
        )
        is_11_field = (
            "Court Name" in row
            and "Location" in row
            and "Location (City)" not in row
            and "Court Name and Location" not in row
            and "Judges / Panel" in row
        )
        is_c4_style = "Court Name and Location" in row

        if is_new_10:
            normalized = {}
            for col in STANDARD_COLUMNS:
                val = row.get(col, "")
                if val is None or str(val).strip() == "":
                    val = ""
                normalized[col] = val

        elif is_c5_style:
            normalized = {
                "Case Name":          row.get("Case Name", ""),
                "Case Number":        row.get("Case Number", ""),
                "Nature of Case":     row.get("Nature of Case", ""),
                "Court Name":         row.get("Court Name", court_name_fallback),
                "Location (City)":    row.get("Location", ""),
                "Judges / Panel":     row.get("Judges/Panel", ""),
                "Courtroom":          row.get("Courtroom", ""),
                "Purpose of Hearing": row.get("Purpose of Hearing", "Oral Argument"),
                "Date":               row.get("Date", ""),
                "Time":               row.get("Time", ""),
            }

        elif is_11_field:
            normalized = {
                "Case Name":          row.get("Case Name", ""),
                "Case Number":        row.get("Case Number", ""),
                "Nature of Case":     row.get("Nature of Case", ""),
                "Court Name":         row.get("Court Name", court_name_fallback),
                "Location (City)":    row.get("Location", ""),
                "Judges / Panel":     row.get("Judges / Panel", ""),
                "Courtroom":          row.get("Courtroom", ""),
                "Purpose of Hearing": row.get("Purpose of Hearing", "Oral Argument"),
                "Date":               row.get("Date", ""),
                "Time":               row.get("Time", ""),
            }

        elif is_c4_style:
            court_and_loc = row.get("Court Name and Location", "")
            if "—" in court_and_loc:
                parts = court_and_loc.split("—", 1)
                court_name = parts[0].strip()
                city = parts[1].strip()
            else:
                court_name = court_and_loc
                city = ""

            normalized = {
                "Case Name":          row.get("Case Name", ""),
                "Case Number":        row.get("Case Number", ""),
                "Nature of Case":     row.get("Nature of Case", ""),
                "Court Name":         court_name_fallback if court_name_fallback else court_name,
                "Location (City)":    city,
                "Judges / Panel":     row.get("Judge Name, Panel", ""),
                "Courtroom":          row.get("Courtroom Number", ""),
                "Purpose of Hearing": row.get("Purpose of Hearing", "Oral Argument"),
                "Date":               row.get("Date", ""),
                "Time":               row.get("Time", ""),
            }

        else:
            normalized = {
                "Case Name":          row.get("Case Name", ""),
                "Case Number":        row.get("Case Number", ""),
                "Nature of Case":     row.get("Nature of Case", ""),
                "Court Name":         court_name_fallback,
                "Location (City)":    row.get("Location", ""),
                "Judges / Panel":     row.get("Judges", ""),
                "Courtroom":          row.get("Courtroom", ""),
                "Purpose of Hearing": row.get("Hearing Purpose", "Oral Argument"),
                "Date":               row.get("Date", ""),
                "Time":               row.get("Time", ""),
            }

        out.append(normalized)
    return out


# ── Full court name lookup ──
COURT_NAMES = {
    "First Circuit":    "United States Court of Appeals for the First Circuit",
    "Second Circuit":   "United States Court of Appeals for the Second Circuit",
    "Third Circuit":    "United States Court of Appeals for the Third Circuit",
    "Fourth Circuit":   "United States Court of Appeals for the Fourth Circuit",
    "Fifth Circuit":    "United States Court of Appeals for the Fifth Circuit",
    "Sixth Circuit":    "United States Court of Appeals for the Sixth Circuit",
    "Seventh Circuit":  "United States Court of Appeals for the Seventh Circuit",
    "Eighth Circuit":   "United States Court of Appeals for the Eighth Circuit",
    "Ninth Circuit":    "United States Court of Appeals for the Ninth Circuit",
    "Tenth Circuit":    "United States Court of Appeals for the Tenth Circuit",
    "Eleventh Circuit": "United States Court of Appeals for the Eleventh Circuit",
    "DC Circuit":       "United States Court of Appeals for the District of Columbia Circuit",
    "Federal Circuit":  "United States Court of Appeals for the Federal Circuit",
}


def update_combined_cases():
    combined = []
    state_keys = [
        ("c1_parsed_cases", "First Circuit"),
        ("c2_cases",        "Second Circuit"),
        ("c3_cases",        "Third Circuit"),
        ("c4_cases",        "Fourth Circuit"),
        ("c5_cases",        "Fifth Circuit"),
        ("c6_cases",        "Sixth Circuit"),
        ("c7_cases",        "Seventh Circuit"),
        ("c8_cases",        "Eighth Circuit"),
        ("c9_cases",        "Ninth Circuit"),
        ("c10_cases",       "Tenth Circuit"),
        ("c11_cases",       "Eleventh Circuit"),
        ("dc_cases",        "DC Circuit"),
        ("cafc_cases",      "Federal Circuit"),
    ]
    for key, label in state_keys:
        data = st.session_state.get(key)
        if data:
            combined.extend(
                _normalize_to_standard(data, COURT_NAMES[label])
            )
    st.session_state.combined_cases = combined


def main():
    st.set_page_config(
        page_title="Federal Court Calendar Analyzer",
        page_icon="⚖️",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # ── Session state init ──
    defaults = {
        'c1_pdf_text': None, 'c1_parsed_cases': None,
        'c2_cases': None,
        'c3_cases': None, 'c3_raw_cases': None,
        'c4_cases': None, 'c4_raw_cases': None,
        'c5_cases': None, 'c5_raw_data': None,
        'c6_cases': None, 'c6_raw_cases': None,
        'c7_cases': None, 'c7_raw_texts': None,
        'c8_cases': None, 'c8_raw_texts': None,
        'c9_cases': None,
        'c10_cases': None, 'c10_raw_data': None,
        'c11_cases': None, 'c11_raw_data': None,
        'dc_cases': None, 'dc_raw_html': None,
        'cafc_cases': None, 'cafc_raw_data': None,
        'combined_cases': None,
        'selected_circuit': "All Circuits",
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

    # ══════════════════════════════════════════════════════════════════════
    #  SIDEBAR
    # ══════════════════════════════════════════════════════════════════════
    with st.sidebar:
        st.title("⚙️ Configuration")

        st.subheader("📍 Select Circuit(s)")
        circuit_option = st.radio(
            "Choose which circuit(s) to scrape:",
            ["First Circuit", "Second Circuit", "Third Circuit",
             "Fourth Circuit", "Fifth Circuit", "Sixth Circuit",
             "Seventh Circuit", "Eighth Circuit", "Ninth Circuit",
             "Tenth Circuit", "Eleventh Circuit", "DC Circuit",
             "Federal Circuit", "All Circuits"],
            index=13,  # "All Circuits"
        )
        st.session_state.selected_circuit = circuit_option

        st.divider()

        # ── First Circuit ──
        if circuit_option in ["First Circuit", "All Circuits"]:
            st.subheader("🔵 First Circuit")
            c1_pdf_url = st.text_input(
                "PDF URL", value=DEFAULT_C1_URL,
                help="URL of the First Circuit Court calendar PDF",
            )
            if st.button("📥 Fetch First Circuit", type="primary", key="fetch_c1"):
                with st.spinner("Fetching First Circuit PDF..."):
                    try:
                        pdf_bytes = fetch_pdf_bytes(c1_pdf_url)
                        st.session_state.c1_pdf_text = extract_text_from_pdf_bytes(pdf_bytes)
                        st.session_state.c1_parsed_cases = parse_calendar_data(
                            st.session_state.c1_pdf_text
                        )
                        st.success(
                            f"✅ First Circuit: {len(st.session_state.c1_parsed_cases)} cases"
                        )
                        update_combined_cases()
                    except Exception as e:
                        st.error(f"❌ Error: {str(e)}")
            st.divider()

        # ── Second Circuit ──
        if circuit_option in ["Second Circuit", "All Circuits"]:
            st.subheader("🔴 Second Circuit")
            col1, col2 = st.columns(2)
            with col1:
                c2_start_month = st.selectbox("Start Month", range(1, 13), index=0, key="c2_sm")
                c2_start_year = st.number_input("Start Year", 2020, 2030, 2026, key="c2_sy")
            with col2:
                c2_end_month = st.selectbox("End Month", range(1, 13), index=2, key="c2_em")
                c2_end_year = st.number_input("End Year", 2020, 2030, 2026, key="c2_ey")
            if st.button("📥 Fetch Second Circuit", type="primary", key="fetch_c2"):
                scraper = USCA2Scraper(verify_ssl=False)
                status_placeholder = st.empty()

                def c2_progress(year, month, event_idx, total_events, total_cases):
                    status_placeholder.info(
                        f"📅 {year}-{month:02d}: Event {event_idx}/{total_events} "
                        f"| Total cases: {total_cases}"
                    )

                with st.spinner("Scraping Second Circuit..."):
                    try:
                        cases = scraper.scrape_date_range(
                            c2_start_year, c2_start_month,
                            c2_end_year, c2_end_month,
                            progress_callback=c2_progress,
                        )
                        st.session_state.c2_cases = cases
                        st.success(f"✅ Second Circuit: {len(cases)} cases")
                        update_combined_cases()
                    except Exception as e:
                        st.error(f"❌ Error: {str(e)}")
            st.divider()

        # ── Third Circuit ──
        if circuit_option in ["Third Circuit", "All Circuits"]:
            st.subheader("🟢 Third Circuit")
            if not HAS_PLAYWRIGHT:
                st.error(
                    "⚠️ Playwright is required for Third Circuit.\n"
                    "```\npip install playwright\nplaywright install chromium\n```"
                )
            else:
                col1, col2 = st.columns(2)
                with col1:
                    c3_start = st.date_input(
                        "Start Date", value=datetime(2026, 2, 1), key="c3_start",
                    )
                with col2:
                    c3_end = st.date_input(
                        "End Date", value=datetime(2026, 3, 31), key="c3_end",
                    )
                if st.button("📥 Fetch Third Circuit", type="primary", key="fetch_c3"):
                    status_placeholder = st.empty()
                    progress_bar = st.progress(0)
                    try:
                        scraper = CA3CourtScraper()
                        cases = run_c3_scraper_in_thread(
                            scraper,
                            c3_start.strftime("%Y-%m-%d"),
                            c3_end.strftime("%Y-%m-%d"),
                            status_placeholder=status_placeholder,
                            progress_bar=progress_bar,
                        )
                        st.session_state.c3_cases = cases
                        st.session_state.c3_raw_cases = scraper.get_raw_data()
                        progress_bar.progress(1.0)
                        status_placeholder.empty()
                        st.success(f"✅ Third Circuit: {len(cases)} cases")
                        update_combined_cases()
                    except Exception as e:
                        st.error(f"❌ Error: {str(e)}")
            st.divider()

        # ── Fourth Circuit ──
        if circuit_option in ["Fourth Circuit", "All Circuits"]:
            st.subheader("🟠 Fourth Circuit")
            c4_session_filter = st.radio(
                "Session types:",
                ["Both", "Regular Only", "Law School & Special Only"],
                index=0, key="c4_session_filter", horizontal=True,
            )
            if st.button("📥 Fetch Fourth Circuit", type="primary", key="fetch_c4"):
                status_placeholder = st.empty()
                progress_bar = st.progress(0)
                try:
                    scraper = USCA4Scraper(verify_ssl=False)

                    def c4_progress(current, total, label):
                        status_placeholder.info(f"📄 Parsing PDF {current}/{total}: {label}")
                        if total > 0:
                            progress_bar.progress(current / total)

                    all_c4 = scraper.scrape_all(progress_callback=c4_progress)

                    if c4_session_filter == "Regular Only":
                        all_c4 = [
                            c for c in all_c4
                            if c.get('Session Type', 'regular') == 'regular'
                        ]
                    elif c4_session_filter == "Law School & Special Only":
                        all_c4 = [
                            c for c in all_c4
                            if c.get('Session Type', 'regular') == 'law_school_special'
                        ]

                    st.session_state.c4_cases = all_c4
                    progress_bar.progress(1.0)
                    status_placeholder.empty()
                    st.success(f"✅ Fourth Circuit: {len(all_c4)} cases")
                    update_combined_cases()
                except Exception as e:
                    st.error(f"❌ Error: {str(e)}")
            st.divider()

        # ── Fifth Circuit ──
        if circuit_option in ["Fifth Circuit", "All Circuits"]:
            st.subheader("🟡 Fifth Circuit")
            c5_delay = st.slider(
                "Request delay (seconds)", 0.5, 5.0, 1.5, 0.5, key="c5_delay",
            )
            if st.button("📥 Fetch Fifth Circuit", type="primary", key="fetch_c5"):
                status_placeholder = st.empty()
                progress_bar = st.progress(0)
                try:
                    scraper = USCA5Scraper(verify_ssl=False, delay=c5_delay)

                    def c5_progress(stage, label, current, total):
                        if stage == "months":
                            status_placeholder.info("🔍 Discovering available months...")
                        elif stage == "hearings":
                            status_placeholder.info(f"📅 Month {current}/{total}: {label}")
                            if total > 0:
                                progress_bar.progress(current / total * 0.3)
                        elif stage == "cases":
                            status_placeholder.info(f"⚖️ Hearing {current}/{total}: {label}")
                            if total > 0:
                                progress_bar.progress(0.3 + current / total * 0.7)

                    all_c5 = scraper.scrape_all(progress_callback=c5_progress)
                    st.session_state.c5_cases = all_c5
                    progress_bar.progress(1.0)
                    status_placeholder.empty()
                    st.success(f"✅ Fifth Circuit: {len(all_c5)} cases")
                    update_combined_cases()
                except Exception as e:
                    st.error(f"❌ Error: {str(e)}")
            st.divider()

        # ── Sixth Circuit ──
        if circuit_option in ["Sixth Circuit", "All Circuits"]:
            st.subheader("🟣 Sixth Circuit")
            st.caption("Dynamically discovers all oral-argument calendar PDFs.")
            if st.button("📥 Fetch Sixth Circuit", type="primary", key="fetch_c6"):
                status_placeholder = st.empty()
                progress_bar = st.progress(0)
                try:
                    scraper = USCA6Scraper(verify_ssl=False)

                    def c6_progress(stage, label, current, total):
                        status_placeholder.info(f"📄 Parsing PDF {current}/{total}: {label}")
                        if total > 0:
                            progress_bar.progress(current / total)

                    all_c6 = scraper.scrape_all(progress_callback=c6_progress)
                    st.session_state.c6_cases = all_c6
                    st.session_state.c6_raw_cases = scraper.get_raw_data()
                    progress_bar.progress(1.0)
                    status_placeholder.empty()
                    st.success(f"✅ Sixth Circuit: {len(all_c6)} cases")
                    update_combined_cases()
                except Exception as e:
                    st.error(f"❌ Error: {str(e)}")
            st.divider()

        # ── Seventh Circuit ──
        if circuit_option in ["Seventh Circuit", "All Circuits"]:
            st.subheader("⚪ Seventh Circuit")
            c7_fetch_daily = st.checkbox(
                "Fetch Daily Argument Calendar", value=True, key="c7_daily",
            )
            c7_fetch_session = st.checkbox(
                "Fetch Week & Session Calendar", value=True, key="c7_session",
            )
            if st.button("📥 Fetch Seventh Circuit", type="primary", key="fetch_c7"):
                status_placeholder = st.empty()
                progress_bar = st.progress(0)
                try:
                    scraper = USCA7Scraper(
                        verify_ssl=False, fetch_daily=c7_fetch_daily,
                        fetch_session=c7_fetch_session,
                    )

                    def c7_progress(current, total, label):
                        status_placeholder.info(
                            f"📄 Downloading & parsing {current}/{total}: {label}"
                        )
                        if total > 0:
                            progress_bar.progress(current / total)

                    all_c7 = scraper.scrape_all(progress_callback=c7_progress)
                    raw_texts = scraper.get_raw_texts()
                    st.session_state.c7_cases = all_c7
                    st.session_state.c7_raw_texts = raw_texts
                    progress_bar.progress(1.0)
                    status_placeholder.empty()
                    st.success(f"✅ Seventh Circuit: {len(all_c7)} records")
                    update_combined_cases()
                except Exception as e:
                    st.error(f"❌ Error: {str(e)}")
            st.divider()

        # ── Eighth Circuit ──
        if circuit_option in ["Eighth Circuit", "All Circuits"]:
            st.subheader("🟤 Eighth Circuit")
            st.caption("Dynamically discovers all argument-calendar PDFs.")
            c8_url = st.text_input(
                "Index page URL", value=DEFAULT_C8_URL, key="c8_url_input",
            )
            if st.button("📥 Fetch Eighth Circuit", type="primary", key="fetch_c8"):
                status_placeholder = st.empty()
                progress_bar = st.progress(0)
                try:
                    scraper = USCA8Scraper(index_url=c8_url, verify_ssl=False)

                    def c8_progress(current, total, label):
                        status_placeholder.info(
                            f"📄 Downloading & parsing PDF {current}/{total}: {label}"
                        )
                        if total > 0:
                            progress_bar.progress(current / total)

                    all_c8 = scraper.scrape_all(progress_callback=c8_progress)
                    raw_texts = scraper.get_raw_texts()
                    st.session_state.c8_cases = all_c8
                    st.session_state.c8_raw_texts = raw_texts
                    progress_bar.progress(1.0)
                    status_placeholder.empty()
                    st.success(f"✅ Eighth Circuit: {len(all_c8)} cases")
                    update_combined_cases()
                except Exception as e:
                    st.error(f"❌ Error: {str(e)}")
            st.divider()

        # ── Ninth Circuit ──
        if circuit_option in ["Ninth Circuit", "All Circuits"]:
            st.subheader("🔶 Ninth Circuit")
            st.caption(
                "Extracts embedded calendar data from the new website. "
                "Single request — no PDF parsing needed."
            )
            if st.button("📥 Fetch Ninth Circuit", type="primary", key="fetch_c9"):
                status_placeholder = st.empty()
                progress_bar = st.progress(0)
                try:
                    scraper = USCA9Scraper()

                    def c9_progress(stage, label, current, total):
                        status_placeholder.info(f"⚖️ {label}")
                        if total > 0:
                            progress_bar.progress(current / total)

                    all_c9 = scraper.scrape(progress_callback=c9_progress)
                    st.session_state.c9_cases = all_c9
                    progress_bar.progress(1.0)
                    status_placeholder.empty()
                    st.success(f"✅ Ninth Circuit: {len(all_c9)} cases")
                    update_combined_cases()
                except Exception as e:
                    st.error(f"❌ Error: {str(e)}")
            st.divider()

        # ── Tenth Circuit ──
        if circuit_option in ["Tenth Circuit", "All Circuits"]:
            st.subheader("🔵 Tenth Circuit")
            st.caption("Discovers calendar events and parses PDF schedules.")
            if st.button("📥 Fetch Tenth Circuit", type="primary", key="fetch_c10"):
                status_placeholder = st.empty()
                progress_bar = st.progress(0)
                try:
                    scraper = USCA10Scraper(verify_ssl=False)

                    def c10_progress(current, total, label):
                        status_placeholder.info(f"📄 Processing {current}/{total}: {label}")
                        if total > 0:
                            progress_bar.progress(current / total)

                    all_c10 = scraper.scrape_all(progress_callback=c10_progress)
                    st.session_state.c10_cases = all_c10
                    st.session_state.c10_raw_data = scraper.get_raw_data()
                    progress_bar.progress(1.0)
                    status_placeholder.empty()
                    st.success(f"✅ Tenth Circuit: {len(all_c10)} cases")
                    update_combined_cases()
                except Exception as e:
                    st.error(f"❌ Error: {str(e)}")
            st.divider()

        # ── Eleventh Circuit ──
        if circuit_option in ["Eleventh Circuit", "All Circuits"]:
            st.subheader("🟢 Eleventh Circuit")
            st.caption("Parses calendars posted 4 weeks before oral argument sessions.")
            if st.button("📥 Fetch Eleventh Circuit", type="primary", key="fetch_c11"):
                status_placeholder = st.empty()
                progress_bar = st.progress(0)
                try:
                    scraper = USCA11Scraper(verify_ssl=False)

                    def c11_progress(current, total, label):
                        status_placeholder.info(f"📄 Parsing PDF {current}/{total}: {label}")
                        if total > 0:
                            progress_bar.progress(current / total)

                    all_c11 = scraper.scrape_all(progress_callback=c11_progress)
                    st.session_state.c11_cases = all_c11
                    st.session_state.c11_raw_data = scraper.get_raw_data()
                    progress_bar.progress(1.0)
                    status_placeholder.empty()
                    st.success(f"✅ Eleventh Circuit: {len(all_c11)} cases")
                    update_combined_cases()
                except Exception as e:
                    st.error(f"❌ Error: {str(e)}")
            st.divider()

        # ── DC Circuit ──
        if circuit_option in ["DC Circuit", "All Circuits"]:
            st.subheader("⚖️ DC Circuit")
            st.caption("Scrapes future oral argument calendar with live audio streaming.")
            if st.button("📥 Fetch DC Circuit", type="primary", key="fetch_dc"):
                status_placeholder = st.empty()
                progress_bar = st.progress(0)
                try:
                    scraper = USCADCScraper(verify_ssl=False)

                    def dc_progress(current, total, label):
                        status_placeholder.info(f"📄 {label}")
                        if total > 0:
                            progress_bar.progress(current / total)

                    all_dc = scraper.scrape_all(progress_callback=dc_progress)
                    st.session_state.dc_cases = all_dc
                    st.session_state.dc_raw_html = scraper.get_raw_html()
                    progress_bar.progress(1.0)
                    status_placeholder.empty()
                    st.success(f"✅ DC Circuit: {len(all_dc)} cases")
                    update_combined_cases()
                except Exception as e:
                    st.error(f"❌ Error: {str(e)}")
            st.divider()

        # ── Federal Circuit ──
        if circuit_option in ["Federal Circuit", "All Circuits"]:
            st.subheader("🔶 Federal Circuit")
            st.caption("Parses scheduled cases from upcoming court sessions.")
            if st.button("📥 Fetch Federal Circuit", type="primary", key="fetch_cafc"):
                status_placeholder = st.empty()
                progress_bar = st.progress(0)
                try:
                    scraper = USCAFCScraper(verify_ssl=False)

                    def cafc_progress(current, total, label):
                        status_placeholder.info(f"📄 Parsing {current}/{total}: {label}")
                        if total > 0:
                            progress_bar.progress(current / total)

                    all_cafc = scraper.scrape_all(progress_callback=cafc_progress)
                    st.session_state.cafc_cases = all_cafc
                    st.session_state.cafc_raw_data = scraper.get_raw_data()
                    progress_bar.progress(1.0)
                    status_placeholder.empty()
                    st.success(f"✅ Federal Circuit: {len(all_cafc)} cases")
                    update_combined_cases()
                except Exception as e:
                    st.error(f"❌ Error: {str(e)}")
            st.divider()

        # ── SSL Warning ──
        st.warning("⚠️ SSL verification is disabled for court website compatibility")

    # ══════════════════════════════════════════════════════════════════════
    #  MAIN CONTENT TABS
    # ══════════════════════════════════════════════════════════════════════
    tabs = st.tabs([
        "📊 Combined",
        "🔵 1st", "🔴 2nd", "🟢 3rd", "🟠 4th", "🟡 5th",
        "🟣 6th", "⚪ 7th", "🟤 8th", "🔶 9th",
        "🔵 10th", "🟢 11th", "⚖️ DC", "🔶 Federal",
    ])

    with tabs[0]:
        display_combined_tab()
    with tabs[1]:
        display_first_circuit_tab()
    with tabs[2]:
        display_second_circuit_tab()
    with tabs[3]:
        display_third_circuit_tab()
    with tabs[4]:
        display_fourth_circuit_tab()
    with tabs[5]:
        display_fifth_circuit_tab()
    with tabs[6]:
        display_sixth_circuit_tab()
    with tabs[7]:
        display_seventh_circuit_tab()
    with tabs[8]:
        display_eighth_circuit_tab()
    with tabs[9]:
        display_ninth_circuit_tab()
    with tabs[10]:
        display_tenth_circuit_tab()
    with tabs[11]:
        display_eleventh_circuit_tab()
    with tabs[12]:
        display_dc_circuit_tab()
    with tabs[13]:
        display_federal_circuit_tab()


if __name__ == "__main__":
    main()