"""
Scraper for the United States Court of Appeals for the Fifth Circuit
Oral Argument Calendar.

Architecture (3-level crawl):
  Level 1 – Calendar home → find month links
  Level 2 – Month page    → summary table with Venue, Dates, Start Time
  Level 3 – Detail page   → session-header rows (date, time, courtroom)
                             + 6-column case table rows

Real URLs:
  Level 1: https://www.ca5.uscourts.gov/oral-argument-information/court-calendars
  Level 2: (linked from Level 1 month names, e.g. July, 2026)
  Level 3: https://www.ca5.uscourts.gov/oral-argument-information/court-calendars/Details/{ID}

Standard 11-field output schema:
  Date | Case Number | Case Name | Nature of Case | Court Name |
  Location | Judges / Panel | Courtroom | Purpose of Hearing |
  Time | Description
"""

import re
import time
import logging
import requests
import pandas as pd
from datetime import datetime
from bs4 import BeautifulSoup
from typing import Optional, Dict, List, Tuple
from urllib.parse import urljoin

# ──────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────
COURT_NAME = "United States Court of Appeals for the Fifth Circuit"
BASE_URL = "https://www.ca5.uscourts.gov"
CALENDAR_HOME = f"{BASE_URL}/oral-argument-information/court-calendars"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    )
}
REQUEST_TIMEOUT = 30

# Regex: session header like "7/7/2026, 9:00 AM,  En Banc Courtroom"
SESSION_HEADER_RE = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{4})"
    r"\s*,\s*"
    r"(\d{1,2}:\d{2}\s*[APap][Mm])"
    r"\s*,\s*"
    r"(.+)",
    re.IGNORECASE,
)

# Parenthetical patterns to strip from case names
CASE_NAME_STRIP_RE = re.compile(
    r"\s*\("
    r"(?:"
    r"[Cc]ons\.?\s*w/.*?"
    r"|"
    r"\d+\s*(?:MINUTES?|MIN)\s+PER\s+SIDE"
    r")"
    r"\)\s*\.?\s*$"
)

# Month name regex for finding month links on Level 1
MONTH_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s*,?\s*\d{4}",
    re.IGNORECASE,
)

# Logger
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

STANDARD_COLUMNS = [
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


# ──────────────────────────────────────────────────────────────────
# Utility helpers
# ──────────────────────────────────────────────────────────────────
def _get(url: str, verify_ssl: bool = True) -> Optional[BeautifulSoup]:
    """Fetch a URL and return a BeautifulSoup object, or None on failure."""
    try:
        resp = requests.get(
            url, headers=HEADERS, timeout=REQUEST_TIMEOUT, verify=verify_ssl
        )
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as exc:
        log.error("Failed to fetch %s: %s", url, exc)
        return None


def _abs_url(href: str) -> str:
    """Convert a relative URL to absolute using urljoin."""
    return urljoin(BASE_URL, href)


def _format_date(raw: str) -> str:
    """Normalize a date string to YYYY-MM-DD."""
    raw = raw.strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def _clean_case_name(name: str) -> str:
    """Clean a case caption: strip parentheticals, trailing period, collapse whitespace."""
    if not name:
        return ""
    name = name.strip()
    for _ in range(5):
        cleaned = CASE_NAME_STRIP_RE.sub("", name).strip()
        if cleaned == name:
            break
        name = cleaned
    name = name.rstrip(".").strip()
    name = re.sub(r"\s+", " ", name)
    return name


def _clean_text(text: str) -> str:
    """Collapse whitespace and strip."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


# ──────────────────────────────────────────────────────────────────
# Level 1 – Calendar Home → Month links
#
# The page has a section "View Hearings by Month" followed by links:
#   September, 2026
#   July, 2026
#   June, 2026
#   May, 2026
# ──────────────────────────────────────────────────────────────────
def _get_month_links(soup: BeautifulSoup) -> List[Tuple[str, str]]:
    """
    From the calendar home page, find all month-level links.
    Returns list of (label, absolute_url).
    """
    month_links: List[Tuple[str, str]] = []

    for a_tag in soup.find_all("a", href=True):
        label = _clean_text(a_tag.get_text())
        if MONTH_RE.search(label):
            href = _abs_url(a_tag["href"])
            # Avoid duplicates
            if href not in [ml[1] for ml in month_links]:
                month_links.append((label, href))

    log.info("Found %d month links on calendar home.", len(month_links))
    return month_links


# ──────────────────────────────────────────────────────────────────
# Level 2 – Month Page → Detail links + blanket metadata
#
# The page has a table:
#   Venue          | Dates      | Start Time | Last Updated
#   En Banc Ctrm   | Jul 7 - 8  | 9:00 AM    | 5/22/2026
#   West Courtroom  | Jul 7 - 9  | 9:00 AM    | 5/21/2026
#
# The Venue cell contains a link to the detail page.
# ──────────────────────────────────────────────────────────────────
def _parse_month_page(
    soup: BeautifulSoup, month_label: str = ""
) -> List[Dict]:
    """
    Parse the month summary table.
    Returns list of dicts with courtroom, dates_label, start_time, detail_url.
    """
    entries: List[Dict] = []

    # Find all tables on the page
    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all(["td", "th"])
            if len(cells) < 3:
                continue

            cell_texts = [_clean_text(c.get_text()) for c in cells]

            # Skip header rows
            first_lower = cell_texts[0].lower()
            if any(kw in first_lower for kw in ("venue", "courtroom", "location")):
                continue

            # Find the hyperlink in this row (the Venue link)
            link = row.find("a", href=True)
            if not link:
                continue

            href = _abs_url(link["href"])
            courtroom = _clean_text(link.get_text()) or cell_texts[0]
            dates_label = cell_texts[1] if len(cell_texts) > 1 else ""
            start_time = cell_texts[2] if len(cell_texts) > 2 else ""

            entries.append(
                {
                    "courtroom": courtroom,
                    "dates_label": dates_label,
                    "start_time": start_time,
                    "detail_url": href,
                    "month_label": month_label,
                }
            )

    # Fallback: look for any links that go to /Details/
    if not entries:
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if "Details" in href or "details" in href:
                label = _clean_text(a_tag.get_text())
                if label:
                    entries.append(
                        {
                            "courtroom": label,
                            "dates_label": "",
                            "start_time": "",
                            "detail_url": _abs_url(href),
                            "month_label": month_label,
                        }
                    )

    log.info("Found %d detail-page entries on month page.", len(entries))
    return entries


# ──────────────────────────────────────────────────────────────────
# Level 3 – Detail Page → Cases
#
# Structure:
#   "Calendar for New Orleans"
#   "July 7 - 8, 2026"
#
#   Session header row: "7/7/2026, 9:00 AM,  En Banc Courtroom"
#   Column header row:  Case Number | Caption | Nature | Special Notes | Origin | Time / Side
#   Case rows:          25-50253    | USA v...| Criminal| ...          | W.D.Tex| 20 min
#
#   Next session header: "7/8/2026, 9:00 AM,  En Banc Courtroom"
#   ... more cases ...
# ──────────────────────────────────────────────────────────────────
def _extract_location_from_page(soup: BeautifulSoup) -> str:
    """
    Try to extract the location from the page header.
    Looks for text like 'Calendar for  New Orleans' or 'Calendar for Houston'.
    """
    page_text = soup.get_text()
    m = re.search(r"Calendar\s+for\s+(.+?)(?:\n|$)", page_text)
    if m:
        loc = _clean_text(m.group(1))
        # Remove trailing date info if present
        loc = re.sub(r"\s*(January|February|March|April|May|June|July|August|"
                     r"September|October|November|December).*$", "", loc, flags=re.IGNORECASE)
        return loc
    return ""


def _parse_detail_page(
    soup: BeautifulSoup,
    fallback_courtroom: str = "",
    fallback_time: str = "",
) -> List[Dict]:
    """
    Parse a detail/calendar page and extract case rows.
    """
    cases: List[Dict] = []

    current_date = ""
    current_time = fallback_time
    current_courtroom = fallback_courtroom

    # Try to get location from page header
    page_location = _extract_location_from_page(soup)

    tables = soup.find_all("table")
    if not tables:
        log.warning("No tables found on detail page.")
        return cases

    # Use the largest table (most rows)
    main_table = max(tables, key=lambda t: len(t.find_all("tr")))

    for row in main_table.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        row_text = _clean_text(row.get_text())

        # ── Check for session header row ──
        # e.g. "7/7/2026, 9:00 AM, En Banc Courtroom"
        m = SESSION_HEADER_RE.match(row_text)
        if m:
            current_date = _format_date(m.group(1))
            current_time = m.group(2).strip()
            current_courtroom = _clean_text(m.group(3))
            log.debug(
                "Session header: date=%s time=%s room=%s",
                current_date,
                current_time,
                current_courtroom,
            )
            continue

        # Check if row is just a date
        date_only_m = re.match(r"^(\d{1,2}/\d{1,2}/\d{4})$", row_text)
        if date_only_m:
            current_date = _format_date(date_only_m.group(1))
            continue

        # ── Skip column-header rows ──
        cell_texts = [_clean_text(c.get_text()) for c in cells]
        joined_lower = " ".join(cell_texts).lower()
        if any(kw in joined_lower for kw in ("caption", "nature", "origin", "case number")):
            continue

        # ── Skip rows that are clearly not case data ──
        if len(cells) < 5:
            continue

        # ── Parse case data rows ──
        case_number = cell_texts[0] if len(cell_texts) > 0 else ""
        case_name_raw = cell_texts[1] if len(cell_texts) > 1 else ""
        nature = cell_texts[2] if len(cell_texts) > 2 else ""
        special_notes = cell_texts[3] if len(cell_texts) > 3 else ""
        origin = cell_texts[4] if len(cell_texts) > 4 else ""
        time_side = cell_texts[5] if len(cell_texts) > 5 else ""

        # Validate: case_number should look like a docket number (e.g. 25-50253)
        if not re.search(r"\d{2}[- ]\d{3,}", case_number):
            continue

        case_name = _clean_case_name(case_name_raw)

        # Build Purpose of Hearing
        purpose_parts = ["Oral Argument"]
        if special_notes:
            purpose_parts.append(special_notes)
        if time_side:
            purpose_parts.append(f"({time_side})")
        purpose = (
            " — ".join(purpose_parts)
            if len(purpose_parts) > 1
            else purpose_parts[0]
        )

        # Use origin as Location, but also note the city from page header
        location = origin if origin else page_location

        cases.append(
            {
                "Date": current_date,
                "Case Number": case_number,
                "Case Name": case_name,
                "Nature of Case": nature,
                "Court Name": COURT_NAME,
                "Location": location,
                "Judges / Panel": "",
                "Courtroom": current_courtroom,
                "Purpose of Hearing": purpose,
                "Time": current_time,
                "Description": "",
            }
        )

    log.info("Parsed %d cases from detail page.", len(cases))
    return cases


# ──────────────────────────────────────────────────────────────────
# DataFrame helpers
# ──────────────────────────────────────────────────────────────────
def _empty_df() -> pd.DataFrame:
    return pd.DataFrame(columns=STANDARD_COLUMNS)


def _to_dataframe(cases: List[Dict]) -> pd.DataFrame:
    if not cases:
        return _empty_df()
    df = pd.DataFrame(cases, columns=STANDARD_COLUMNS)
    df.drop_duplicates(inplace=True)
    df.sort_values(by=["Date", "Case Number"], inplace=True, ignore_index=True)
    log.info("Total cases scraped: %d", len(df))
    return df


# ──────────────────────────────────────────────────────────────────
# Class interface — matches what app.py expects:
#
#   scraper = USCA5Scraper(verify_ssl=False, delay=1.5)
#   results = scraper.scrape_all(progress_callback=fn)
#
#   progress_callback signature:
#     def fn(stage: str, label: str, current: int, total: int)
#     stages: "months", "hearings", "cases"
#
#   Returns: List[Dict] with the standard 11-field schema
# ──────────────────────────────────────────────────────────────────
class USCA5Scraper:
    """Fifth Circuit oral argument calendar scraper."""

    def __init__(self, verify_ssl=True, delay=1.5, **kwargs):
        self.verify_ssl = verify_ssl
        self.delay = delay
        self.court_name = COURT_NAME
        self._raw_data = None

    def scrape_all(self, progress_callback=None) -> List[Dict]:
        """
        Run the 3-level crawl and return a list of case dicts.

        Args:
            progress_callback: Optional function(stage, label, current, total)

        Returns:
            List of dicts, each with the 11 standard fields.
        """
        all_cases: List[Dict] = []

        # ── Level 1: Calendar Home ──
        if progress_callback:
            progress_callback("months", "Fetching calendar home...", 0, 0)

        log.info("Fetching calendar home: %s", CALENDAR_HOME)
        home_soup = _get(CALENDAR_HOME, verify_ssl=self.verify_ssl)
        if not home_soup:
            log.error("Could not fetch calendar home page.")
            return []

        month_links = _get_month_links(home_soup)
        if not month_links:
            log.warning("No month links found on calendar home.")
            return []

        if progress_callback:
            progress_callback(
                "months",
                f"Found {len(month_links)} months",
                len(month_links),
                len(month_links),
            )

        # ── Level 2: Month Pages ──
        all_detail_entries: List[Dict] = []
        for m_idx, (month_label, month_url) in enumerate(month_links, 1):
            if progress_callback:
                progress_callback(
                    "hearings", month_label, m_idx, len(month_links)
                )

            log.info("Fetching month page: %s → %s", month_label, month_url)
            month_soup = _get(month_url, verify_ssl=self.verify_ssl)
            if not month_soup:
                continue

            detail_entries = _parse_month_page(month_soup, month_label=month_label)
            if not detail_entries:
                log.warning("No detail entries found for %s.", month_label)
                # Try parsing the month page itself as a detail page
                cases = _parse_detail_page(month_soup)
                if cases:
                    all_cases.extend(cases)
                continue

            all_detail_entries.extend(detail_entries)
            time.sleep(self.delay)

        # ── Level 3: Detail Pages ──
        for d_idx, entry in enumerate(all_detail_entries, 1):
            detail_label = (
                entry.get("month_label", "")
                + " — "
                + entry.get("courtroom", "")
            )
            if progress_callback:
                progress_callback(
                    "cases", detail_label, d_idx, len(all_detail_entries)
                )

            log.info(
                "Fetching detail page: %s → %s",
                entry["courtroom"],
                entry["detail_url"],
            )
            detail_soup = _get(entry["detail_url"], verify_ssl=self.verify_ssl)
            if not detail_soup:
                continue

            cases = _parse_detail_page(
                detail_soup,
                fallback_courtroom=entry["courtroom"],
                fallback_time=entry["start_time"],
            )
            all_cases.extend(cases)
            time.sleep(self.delay)

        # Deduplicate by (Case Number, Date)
        seen = set()
        unique = []
        for c in all_cases:
            key = (c.get("Case Number", ""), c.get("Date", ""))
            if key not in seen:
                seen.add(key)
                unique.append(c)

        self._raw_data = unique
        log.info("USCA5Scraper: Total unique cases: %d", len(unique))
        return unique

    def scrape(self) -> pd.DataFrame:
        """Run the scraper and return a DataFrame."""
        cases = self.scrape_all()
        return _to_dataframe(cases)

    def get_raw_data(self):
        """Return raw scraped data for debugging."""
        return self._raw_data


# ──────────────────────────────────────────────────────────────────
# Legacy function entry point (for standalone use)
# ──────────────────────────────────────────────────────────────────
def scrape_fifth_circuit() -> pd.DataFrame:
    """Convenience function that uses the class internally."""
    scraper = USCA5Scraper()
    cases = scraper.scrape_all()
    return _to_dataframe(cases)


# ──────────────────────────────────────────────────────────────────
# Standalone execution
# ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    df = scrape_fifth_circuit()
    if df.empty:
        print("No cases found.")
    else:
        print(f"\n{'=' * 80}")
        print(f"  5th Circuit Oral Argument Calendar — {len(df)} cases")
        print(f"{'=' * 80}\n")
        print(df.to_string(index=False))
        out_file = "circuit5_oral_arguments.csv"
        df.to_csv(out_file, index=False)
        print(f"\nExported to {out_file}")