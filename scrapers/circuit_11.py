"""
Scraper for the United States Court of Appeals for the Eleventh Circuit.

The Eleventh Circuit publishes oral argument calendars as PDF documents
at ca11.uscourts.gov/oral-argument-calendars. Calendars are posted
4 weeks before oral argument sessions and revised as necessary.

PDF format:
    - Cover page with date range, location, courtroom
    - Judge panels: "Before HONS. JUDGE_A, JUDGE_B, and JUDGE_C, Circuit Judges."
    - Date headers: "MONDAY, JANUARY 13, 2026"
    - Case lines: "23-12345** Case Name v. Other Party"
      * = extended time (20 min/side)
      ** = submitted on briefs
      + = opinion issued

Output: 11-field standardized schema.
"""

import re
import ssl
import logging
import time
from datetime import datetime
from io import BytesIO
from typing import List, Dict, Optional, Callable
from urllib.parse import urljoin

import urllib3
import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup

# Suppress SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Detect PDF engine
try:
    import pdfplumber
    PDF_ENGINE = "pdfplumber"
except ImportError:
    try:
        from PyPDF2 import PdfReader
        PDF_ENGINE = "pypdf2"
    except ImportError:
        PDF_ENGINE = None

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

C11_COURT_NAME = "United States Court of Appeals for the Eleventh Circuit"
C11_BASE_URL = "https://www.ca11.uscourts.gov"
C11_CALENDAR_URL = f"{C11_BASE_URL}/oral-argument-calendars"

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_DAYS = r"(?:MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY)"
_MONTHS = (
    r"(?:JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|"
    r"SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)"
)

# Date heading: "MONDAY, JANUARY 13, 2026"
C11_RE_DATE_HEADING = re.compile(
    rf"({_DAYS}),\s+({_MONTHS}\s+\d{{1,2}},?\s+\d{{4}})",
    re.IGNORECASE,
)

# Judge panel: "Before HONS. Judge_A, Judge_B, and Judge_C, Circuit Judges."
C11_RE_JUDGE_PANEL = re.compile(
    r"Before\s+HONS?\.\s+(.+?)(?:,\s*Circuit Judges?\.?)",
    re.IGNORECASE,
)

# Case line: "23-12345** Case Name v. Other Party"
C11_RE_CASE_LINE = re.compile(
    r"^(\d{2}-\d{4,5})(\*{0,2})(\+?)\s+(.+)",
    re.MULTILINE,
)

# Consolidated: "(Consolidated with 23-12346, Other Case Name)"
C11_RE_CONSOLIDATED = re.compile(
    r"\(Consolidated with\s+([\d\-]+),\s*(.+?)\)",
    re.IGNORECASE,
)

# Courtroom: "Courtroom 339"
C11_RE_COURTROOM = re.compile(r"Courtroom\s+(\d+)", re.IGNORECASE)

# Link metadata: date range
C11_RE_DATE_RANGE = re.compile(
    r"([A-Z][a-z]+ \d{1,2},?\s*\d{4})\s*to\s*([A-Z][a-z]+ \d{1,2},?\s*\d{4})",
)

# Link metadata: location
C11_RE_LINK_LOCATION = re.compile(
    r"-\s*([A-Za-z\s]+,\s*[A-Za-z\s]+?)(?:\s*\()",
)

# Link metadata: calendar number
C11_RE_CALENDAR_NUM = re.compile(
    r"Calendar\s*#?\s*(\d+)",
    re.IGNORECASE,
)

# Date normalization
C11_RE_DATE_SHORT = re.compile(
    r"((?:JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|"
    r"SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)

# Location normalization
C11_LOCATION_MAP = {
    "atlanta":       "Atlanta, GA",
    "miami":         "Miami, FL",
    "jacksonville":  "Jacksonville, FL",
    "montgomery":    "Montgomery, AL",
    "tampa":         "Tampa, FL",
    "orlando":       "Orlando, FL",
    "fort lauderdale": "Fort Lauderdale, FL",
}


# ---------------------------------------------------------------------------
# SSL Bypass Adapter
# ---------------------------------------------------------------------------

class SSLBypassAdapter(HTTPAdapter):
    """Adapter that fully disables SSL verification."""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)

    def send(self, request, **kwargs):
        kwargs["verify"] = False
        return super().send(request, **kwargs)


# ---------------------------------------------------------------------------
# Main Scraper Class
# ---------------------------------------------------------------------------

class USCA11Scraper:
    """
    Scraper for the Eleventh Circuit Court of Appeals oral argument calendar.

    Usage:
        scraper = USCA11Scraper()
        cases = scraper.scrape()  # Returns List[Dict] with 11-field schema
    """

    def __init__(
        self,
        calendar_url: str = C11_CALENDAR_URL,
        base_url: str = C11_BASE_URL,
        verify_ssl: bool = False,
        request_delay: float = 1.0,
        max_retries: int = 3,
        timeout: int = 30,
    ):
        self.calendar_url = calendar_url
        self.base_url = base_url
        self.verify_ssl = verify_ssl
        self.request_delay = request_delay
        self.max_retries = max_retries
        self.timeout = timeout
        self.raw_data: List[Dict] = []

        self.session = self._build_session()

    def _build_session(self) -> requests.Session:
        """Build session with SSL bypass."""
        session = requests.Session()
        session.verify = False
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        adapter = SSLBypassAdapter()
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    # -----------------------------------------------------------------------
    # HTTP helpers
    # -----------------------------------------------------------------------

    def _get(self, url: str, timeout: int = None) -> Optional[requests.Response]:
        """GET with delay and error handling."""
        try:
            time.sleep(self.request_delay)
            resp = self.session.get(url, timeout=timeout or self.timeout)
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as exc:
            logger.error("[C11] Request failed for %s: %s", url, exc)
            return None

    def _resolve_url(self, href: str) -> str:
        """Resolve relative URLs."""
        if href.startswith("http"):
            return href
        return self.base_url + (href if href.startswith("/") else f"/{href}")

    # -----------------------------------------------------------------------
    # Discovery: find calendar PDFs
    # -----------------------------------------------------------------------

    def _parse_link_metadata(self, text: str) -> Dict:
        """Extract metadata from PDF link text."""
        meta = {}

        m = C11_RE_DATE_RANGE.search(text)
        if m:
            meta["date_range"] = f"{m.group(1).strip()} to {m.group(2).strip()}"

        m = C11_RE_LINK_LOCATION.search(text)
        if m:
            meta["location"] = m.group(1).strip()

        m = C11_RE_CALENDAR_NUM.search(text)
        if m:
            meta["calendar_number"] = m.group(1)

        return meta

    def discover_calendars(self) -> List[Dict]:
        """
        Scrape the calendar page for PDF links.

        Returns list of dicts with keys:
            title, pdf_url, date_range, location, calendar_number
        """
        logger.info("[C11] Fetching calendar page: %s", self.calendar_url)

        resp = self._get(self.calendar_url)
        if not resp:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        calendars = []
        seen_urls = set()

        for a_tag in soup.find_all("a", href=True):
            href = a_tag.get("href", "")
            text = a_tag.get_text(" ", strip=True)

            is_calendar_pdf = False

            # Pattern 1: oral_arguments path with .pdf
            if "oral_arguments" in href and ".pdf" in href.lower():
                is_calendar_pdf = True

            # Pattern 2: default files path with calendar-like text
            if (
                "/sites/default/files/" in href
                and ".pdf" in href.lower()
                and re.search(r"\d{4}\s*to\s*", text)
                and re.search(r"calendar", text, re.IGNORECASE)
            ):
                is_calendar_pdf = True

            if not is_calendar_pdf:
                continue

            full_url = self._resolve_url(href)
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)

            meta = self._parse_link_metadata(text)

            calendars.append({
                "title": text,
                "pdf_url": full_url,
                "date_range": meta.get("date_range", ""),
                "location": meta.get("location", ""),
                "calendar_number": meta.get("calendar_number", ""),
            })

        logger.info("[C11] Found %d calendar PDF(s)", len(calendars))
        return calendars

    # -----------------------------------------------------------------------
    # PDF extraction
    # -----------------------------------------------------------------------

    def _download_pdf_text(self, pdf_url: str) -> str:
        """Download PDF and extract text."""
        logger.info("[C11] Downloading: %s", pdf_url)

        resp = self._get(pdf_url, timeout=60)
        if not resp:
            return ""

        raw = BytesIO(resp.content)

        if PDF_ENGINE == "pdfplumber":
            import pdfplumber
            try:
                with pdfplumber.open(raw) as pdf:
                    return "\n".join(page.extract_text() or "" for page in pdf.pages)
            except Exception as exc:
                logger.error("[C11] pdfplumber error: %s", exc)
                return ""

        elif PDF_ENGINE == "pypdf2":
            from PyPDF2 import PdfReader
            try:
                reader = PdfReader(raw)
                return "\n".join(page.extract_text() or "" for page in reader.pages)
            except Exception as exc:
                logger.error("[C11] PyPDF2 error: %s", exc)
                return ""

        else:
            logger.error("[C11] No PDF library available. Install pdfplumber or PyPDF2.")
            return ""

    # -----------------------------------------------------------------------
    # PDF parsing
    # -----------------------------------------------------------------------

    def _parse_flags(self, asterisks: str, plus: str) -> List[str]:
        """Convert flag symbols to human-readable labels."""
        flags = []
        if "**" in asterisks:
            flags.append("BRIEFS_ONLY")
        elif "*" in asterisks:
            flags.append("EXTENDED_TIME")
        if "+" in plus:
            flags.append("OPINION_ISSUED")
        return flags

    def _derive_purpose(self, flags: List[str]) -> str:
        """Derive Purpose of Hearing from flags."""
        if "BRIEFS_ONLY" in flags:
            return "Submitted on Briefs"
        return "Oral Argument"

    def _build_description(
        self,
        flags: List[str],
        consolidated_with: Optional[str],
        calendar_number: str,
    ) -> str:
        """Build description from flags, consolidation info, and calendar number."""
        parts = []

        if "EXTENDED_TIME" in flags:
            parts.append("Extended time (20 min/side)")
        if "BRIEFS_ONLY" in flags:
            parts.append("Submitted on briefs")
        if "OPINION_ISSUED" in flags:
            parts.append("Opinion issued")
        if consolidated_with:
            parts.append(f"Consolidated with {consolidated_with}")
        if calendar_number:
            parts.append(f"Calendar #{calendar_number}")

        return "; ".join(parts)

    def _parse_pdf_cases(
        self,
        raw_text: str,
        calendar_info: Dict,
    ) -> List[Dict]:
        """
        Parse structured case data from PDF text.

        Walks through the text tracking context:
          - Judge panel (from "Before HONS." lines)
          - Date (from day-of-week headers)
          - Courtroom (from "Courtroom N" lines)

        Returns raw case dicts.
        """
        cases = []

        # Extract courtroom from full text
        courtroom = ""
        cr_m = C11_RE_COURTROOM.search(raw_text)
        if cr_m:
            courtroom = f"Courtroom {cr_m.group(1)}"

        # Also check calendar title for courtroom
        if not courtroom and calendar_info.get("title"):
            cr_m = C11_RE_COURTROOM.search(calendar_info["title"])
            if cr_m:
                courtroom = f"Courtroom {cr_m.group(1)}"

        # Split into judge panels
        panel_splits = list(C11_RE_JUDGE_PANEL.finditer(raw_text))

        for idx, panel_match in enumerate(panel_splits):
            # Extract judges
            judges_raw = panel_match.group(1)
            judges = [
                j.strip()
                for j in re.split(r",\s*and\s+|,\s+|\s+and\s+", judges_raw)
                if j.strip()
            ]
            judges_str = ", ".join(judges)

            # Get panel text block
            start = panel_match.end()
            end = (
                panel_splits[idx + 1].start()
                if idx + 1 < len(panel_splits)
                else len(raw_text)
            )
            panel_text = raw_text[start:end]

            # Split into hearing days
            day_splits = list(C11_RE_DATE_HEADING.finditer(panel_text))

            for d_idx, day_match in enumerate(day_splits):
                day_of_week = day_match.group(1).strip()
                date_str = day_match.group(2).strip()

                d_start = day_match.end()
                d_end = (
                    day_splits[d_idx + 1].start()
                    if d_idx + 1 < len(day_splits)
                    else len(panel_text)
                )
                day_text = panel_text[d_start:d_end]

                # Join continuation lines
                joined_lines = []
                for line in day_text.splitlines():
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if re.match(r"^\d{2}-\d{4,5}", stripped):
                        joined_lines.append(stripped)
                    elif joined_lines:
                        joined_lines[-1] += " " + stripped

                # Parse each case
                for case_line in joined_lines:
                    m = C11_RE_CASE_LINE.match(case_line)
                    if not m:
                        continue

                    case_number = m.group(1)
                    asterisks = m.group(2)
                    plus_sign = m.group(3)
                    case_title_raw = m.group(4).strip()

                    flags = self._parse_flags(asterisks, plus_sign)

                    # Check for consolidated cases
                    consolidated_with = None
                    cons_match = C11_RE_CONSOLIDATED.search(case_title_raw)
                    if cons_match:
                        consolidated_with = (
                            f"{cons_match.group(1)} - "
                            f"{cons_match.group(2).strip().rstrip(')')}"
                        )
                        case_title_clean = (
                            case_title_raw[: cons_match.start()]
                            .strip()
                            .rstrip("(")
                            .strip()
                        )
                    else:
                        case_title_clean = case_title_raw

                    cases.append({
                        "case_number": case_number,
                        "case_name": case_title_clean,
                        "date": date_str,
                        "day_of_week": day_of_week,
                        "judges": judges_str,
                        "courtroom": courtroom,
                        "location": calendar_info.get("location", ""),
                        "calendar_number": calendar_info.get("calendar_number", ""),
                        "flags": flags,
                        "consolidated_with": consolidated_with,
                        "purpose": self._derive_purpose(flags),
                        "description": self._build_description(
                            flags, consolidated_with,
                            calendar_info.get("calendar_number", ""),
                        ),
                    })

        logger.info("[C11]   Parsed %d cases from PDF", len(cases))
        return cases

    # -----------------------------------------------------------------------
    # Normalization
    # -----------------------------------------------------------------------

    @staticmethod
    def _format_date(raw_date: str) -> str:
        """Convert date string to ISO YYYY-MM-DD format."""
        if not raw_date:
            return ""
        raw_date = raw_date.strip()

        # Already ISO
        if re.match(r"^\d{4}-\d{2}-\d{2}$", raw_date):
            return raw_date

        for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(raw_date, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue

        # Try extracting from within a longer string
        m = C11_RE_DATE_SHORT.search(raw_date)
        if m:
            for fmt in ("%B %d, %Y", "%B %d %Y"):
                try:
                    return datetime.strptime(m.group(1), fmt).strftime("%Y-%m-%d")
                except ValueError:
                    continue

        return raw_date

    @staticmethod
    def _normalize_location(raw_location: str) -> str:
        """Normalize location to 'City, ST' format."""
        if not raw_location:
            return ""

        loc = raw_location.strip()
        lower = loc.lower()

        for key, normalized in C11_LOCATION_MAP.items():
            if key in lower:
                return normalized

        # Try to clean up "City, State" format
        if "," in loc:
            parts = loc.split(",")
            city = parts[0].strip().title()
            state = parts[1].strip()
            # Abbreviate state if it's a full name
            state_abbrevs = {
                "georgia": "GA", "florida": "FL", "alabama": "AL",
            }
            state_lower = state.lower()
            if state_lower in state_abbrevs:
                state = state_abbrevs[state_lower]
            return f"{city}, {state}"

        return loc

    def _to_standard_schema(self, raw_cases: List[Dict]) -> List[Dict]:
        """
        Map raw parsed records to the standard 11-field output schema.

        Fields:
          1.  Case Name
          2.  Case Number
          3.  Nature of Case
          4.  Court Name
          5.  Location
          6.  Judges / Panel
          7.  Courtroom
          8.  Purpose of Hearing
          9.  Date
          10. Time
          11. Description
        """
        normalized = []
        for r in raw_cases:
            normalized.append({
                "Case Name":          r.get("case_name", "").strip(),
                "Case Number":        r.get("case_number", "").strip(),
                "Nature of Case":     "",  # Not available in C11 PDFs
                "Court Name":         C11_COURT_NAME,
                "Location":           self._normalize_location(r.get("location", "")),
                "Judges / Panel":     r.get("judges", "").strip(),
                "Courtroom":          r.get("courtroom", "").strip(),
                "Purpose of Hearing": r.get("purpose", "Oral Argument"),
                "Date":               self._format_date(r.get("date", "")),
                "Time":               "",  # Not specified per-case in C11 PDFs
                "Description":        r.get("description", "").strip(),
            })

        return normalized

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def scrape(
        self,
        progress_callback: Optional[Callable] = None,
    ) -> List[Dict]:
        """
        Main entry point. Scrapes all Eleventh Circuit oral argument calendars.

        Returns a list of dicts in the standard 11-field schema.

        progress_callback signature: (current: int, total: int, label: str)
        """
        return self.scrape_all(progress_callback)

    def scrape_all(
        self,
        progress_callback: Optional[Callable] = None,
    ) -> List[Dict]:
        """Scrape all available calendar PDFs."""
        if PDF_ENGINE is None:
            logger.error("[C11] No PDF library available. Install pdfplumber or PyPDF2.")
            return []

        # Step 1: Discover calendar PDFs
        calendars = self.discover_calendars()
        if not calendars:
            logger.warning("[C11] No calendar PDFs found")
            return []

        total = len(calendars)
        logger.info("[C11] Found %d calendar PDF(s) to process", total)

        # Step 2: Download and parse each PDF
        all_raw = []

        for idx, cal in enumerate(calendars, 1):
            if progress_callback:
                progress_callback(idx, total, cal.get("title", "")[:60])

            raw_text = self._download_pdf_text(cal["pdf_url"])
            if raw_text:
                self.raw_data.append({"calendar": cal, "text": raw_text})

                cases = self._parse_pdf_cases(raw_text, cal)
                all_raw.extend(cases)
                logger.info(
                    "[C11] PDF %d/%d: %d cases from %s",
                    idx, total, len(cases), cal.get("title", "")[:50],
                )

        # Step 3: Normalize to standard schema
        normalized = self._to_standard_schema(all_raw)

        # Final deduplication
        seen = set()
        unique = []
        for c in normalized:
            key = (c["Case Number"], c["Date"])
            if key not in seen:
                seen.add(key)
                unique.append(c)

        logger.info(
            "[C11] Final: %d cases (%d before dedup)",
            len(unique), len(normalized),
        )

        if progress_callback:
            progress_callback(total, total, f"Complete — {len(unique)} cases")

        return unique

    def get_raw_data(self) -> List[Dict]:
        """Return raw PDF text data for debugging."""
        return self.raw_data


# ---------------------------------------------------------------------------
# CLI entry point for testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    scraper = USCA11Scraper()

    print("=" * 70)
    print("ELEVENTH CIRCUIT SCRAPER — STANDARDIZED")
    print("=" * 70)

    if PDF_ENGINE:
        print(f"PDF engine: {PDF_ENGINE}")
    else:
        print("ERROR: No PDF library found. Install pdfplumber or PyPDF2.")
        exit(1)

    def cli_progress(current, total, label):
        pct = (current / total * 100) if total else 0
        print(f"  {pct:5.1f}% — {label}")

    cases = scraper.scrape(progress_callback=cli_progress)

    print(f"\nTotal cases: {len(cases)}")

    if cases:
        print("\n--- Sample Cases ---")
        for i, c in enumerate(cases[:3], 1):
            print(f"\nCase {i}:")
            for k, v in c.items():
                print(f"  {k:20s}: {v}")

        print("\n--- Field Completeness ---")
        fields = [
            "Case Name", "Case Number", "Nature of Case", "Court Name",
            "Location", "Judges / Panel", "Courtroom", "Purpose of Hearing",
            "Date", "Time", "Description",
        ]
        for field in fields:
            filled = sum(1 for c in cases if c.get(field))
            pct = (filled / len(cases) * 100) if cases else 0
            bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
            print(f"  {field:20s}: {bar} {filled}/{len(cases)} ({pct:.0f}%)")