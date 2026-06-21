"""
Scraper for the United States Court of Appeals for the Tenth Circuit.

The Tenth Circuit publishes oral argument calendars as PDF documents
linked from event pages at ca10.uscourts.gov/calendar.

Structure:
    /calendar                          → Main calendar page with session links
    /calendar/event/<slug>             → Event page with PDF links
    /sites/ca10/files/.../name.pdf     → PDF with case-level data

The court has five regular terms per year (Jan, Mar, May, Sep, Nov),
plus occasional special sessions. Arguments are almost always in Denver
at the Byron White Courthouse.

PDF format (typical):
    - Header with courtroom, date, session time
    - Judge panel ("Before: JUDGE_A, JUDGE_B, and JUDGE_C, Circuit Judges")
    - Case rows with case number, case name, time allotment
    - Cases marked "Submit" are submitted on briefs (no oral argument)

Output: 11-field standardized schema.
"""

import re
import ssl
import logging
import time
from datetime import datetime
from urllib.parse import urljoin
from io import BytesIO
from typing import List, Dict, Optional, Callable, Tuple

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
        import fitz  # PyMuPDF
        PDF_ENGINE = "pymupdf"
    except ImportError:
        PDF_ENGINE = None

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

C10_COURT_NAME = "United States Court of Appeals for the Tenth Circuit"
C10_BASE_URL = "https://www.ca10.uscourts.gov"
C10_CALENDAR_URL = f"{C10_BASE_URL}/calendar"
C10_DEFAULT_LOCATION = "Denver, CO"
C10_DEFAULT_COURTHOUSE = "Byron White United States Courthouse"

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Case number: 23-1234, 24-12345
C10_RE_CASE_NUM = re.compile(r"\b(\d{2}-\d{3,5})\b")

# Date patterns in PDFs
C10_RE_DATE_FULL = re.compile(
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
    r",?\s+"
    r"((?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)
C10_RE_DATE_SHORT = re.compile(
    r"((?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)

# Time pattern: "9:00 a.m.", "10:00 AM", "1:30 P.M."
C10_RE_TIME = re.compile(
    r"(\d{1,2}:\d{2}\s*(?:[AaPp]\.?[Mm]\.?))",
)

# Session header: "9:00 a.m. Session" or "Courtroom 1 — 9:00 a.m."
C10_RE_SESSION = re.compile(
    r"(\d{1,2}:\d{2}\s*(?:[AaPp]\.?[Mm]\.?))\s*(?:Session|Calendar|Argument)",
    re.IGNORECASE,
)

# Courtroom: "Courtroom 1", "Courtroom A", "Courtroom IV"
C10_RE_COURTROOM = re.compile(
    r"Courtroom\s+(\w+)",
    re.IGNORECASE,
)

# "Before:" line for judges
C10_RE_BEFORE = re.compile(
    r"Before[:\s]+(.+?)(?:\s*,?\s*(?:Circuit|Senior|Chief)\s+Judges?\.?\s*$|$)",
    re.IGNORECASE | re.MULTILINE,
)

# "v." pattern for case names
C10_RE_VS = re.compile(r"\s+v\.?\s+", re.IGNORECASE)

# Submit / SOB markers
C10_RE_SUBMIT = re.compile(
    r"\b(?:submit(?:ted)?|s\.?o\.?b\.?|on\s+(?:the\s+)?briefs)\b",
    re.IGNORECASE,
)

# Time allotment: "15 min", "20 minutes", "15/15"
C10_RE_TIME_ALLOT = re.compile(
    r"\b(\d{1,2})\s*(?:min(?:utes?)?|/\d{1,2})\b",
    re.IGNORECASE,
)

# Event page date range in title: "January 2026 Term" or "January 26, 2026 Special Session"
C10_RE_EVENT_TITLE_DATE = re.compile(
    r"((?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(?:\d{1,2},?\s+)?\d{4})",
    re.IGNORECASE,
)

# Location from event page text
C10_RE_LOCATION = re.compile(
    r"Location\(?s?\)?[:\s]+(.+?)(?:\s*(?:Associated|Last\s+Updated|$))",
    re.IGNORECASE,
)

# PDF link
C10_RE_PDF_LINK = re.compile(r"\.pdf$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# SSL Bypass Adapter
# ---------------------------------------------------------------------------

class NoSSLAdapter(HTTPAdapter):
    """HTTPAdapter that disables SSL verification."""

    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(connections, maxsize, block=block, **kwargs)

    def send(self, request, stream=False, timeout=None, verify=False,
             cert=None, proxies=None):
        return super().send(
            request, stream=stream, timeout=timeout,
            verify=False, cert=cert, proxies=proxies,
        )


# ---------------------------------------------------------------------------
# Main Scraper Class
# ---------------------------------------------------------------------------

class USCA10Scraper:
    """
    Scraper for the Tenth Circuit Court of Appeals oral argument calendar.

    Usage:
        scraper = USCA10Scraper()
        cases = scraper.scrape()  # Returns List[Dict] with 11-field schema
    """

    def __init__(
        self,
        calendar_url: str = C10_CALENDAR_URL,
        base_url: str = C10_BASE_URL,
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
        adapter = NoSSLAdapter(max_retries=self.max_retries)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    # -----------------------------------------------------------------------
    # HTTP helpers
    # -----------------------------------------------------------------------

    def _get(self, url: str) -> Optional[requests.Response]:
        """GET with delay and error handling."""
        try:
            time.sleep(self.request_delay)
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as exc:
            logger.error("[C10] Request failed for %s: %s", url, exc)
            return None

    def _get_bytes(self, url: str) -> Optional[bytes]:
        """GET returning raw bytes (for PDFs)."""
        try:
            time.sleep(self.request_delay)
            resp = self.session.get(url, timeout=60)
            resp.raise_for_status()
            return resp.content
        except requests.exceptions.RequestException as exc:
            logger.error("[C10] PDF download failed for %s: %s", url, exc)
            return None

    # -----------------------------------------------------------------------
    # Discovery: find event pages and PDF links
    # -----------------------------------------------------------------------

    def discover_events(self) -> List[Dict]:
        """
        Discover calendar events from the main calendar page.

        The calendar page at /calendar lists session links like:
            /calendar/event/january-2026-term-court
            /calendar/event/january-26-2026-special-session

        Each event page contains PDF links with case-level data.

        Returns a list of event dicts with keys:
            title, event_url, date_range, location, pdf_urls
        """
        logger.info("[C10] Fetching calendar page: %s", self.calendar_url)

        resp = self._get(self.calendar_url)
        if not resp:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        events = []
        seen_urls = set()

        # Strategy 1: Find event links (e.g., /calendar/event/...)
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            full_url = urljoin(self.base_url, href)

            if "/calendar/event/" in href and full_url not in seen_urls:
                seen_urls.add(full_url)
                title = a_tag.get_text(strip=True) or ""
                events.append({
                    "title": title,
                    "event_url": full_url,
                    "date_range": "",
                    "location": "",
                    "pdf_urls": [],
                })

        # Strategy 2: Find direct PDF links on the calendar page
        direct_pdfs = []
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if C10_RE_PDF_LINK.search(href):
                pdf_url = urljoin(self.base_url, href)
                if pdf_url not in seen_urls:
                    seen_urls.add(pdf_url)
                    direct_pdfs.append(pdf_url)

        # For direct PDFs, try to find their parent container for context
        if direct_pdfs and not events:
            for pdf_url in direct_pdfs:
                events.append({
                    "title": pdf_url.split("/")[-1].replace(".pdf", ""),
                    "event_url": self.calendar_url,
                    "date_range": "",
                    "location": "",
                    "pdf_urls": [pdf_url],
                })

        logger.info("[C10] Found %d event links on calendar page", len(events))

        # Fetch each event page to get PDF links and metadata
        for event in events:
            if not event["pdf_urls"]:
                self._enrich_event(event)

        return events

    def _enrich_event(self, event: Dict):
        """
        Fetch an event page and extract PDF links, date range, and location.
        """
        url = event["event_url"]
        logger.info("[C10] Fetching event page: %s", url)

        resp = self._get(url)
        if not resp:
            return

        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(" ", strip=True)

        # Extract PDF links
        pdf_urls = []
        seen = set()
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if C10_RE_PDF_LINK.search(href):
                pdf_url = urljoin(self.base_url, href)
                if pdf_url not in seen:
                    seen.add(pdf_url)
                    pdf_urls.append(pdf_url)

        event["pdf_urls"] = pdf_urls

        # Extract date range from page
        if not event["date_range"]:
            # Look in strong tags first
            for strong in soup.find_all("strong"):
                strong_text = strong.get_text(strip=True)
                m = C10_RE_DATE_FULL.search(strong_text)
                if m:
                    event["date_range"] = strong_text
                    break
            # Fallback: title
            if not event["date_range"] and event["title"]:
                m = C10_RE_EVENT_TITLE_DATE.search(event["title"])
                if m:
                    event["date_range"] = m.group(1)

        # Extract location
        if not event["location"]:
            m = C10_RE_LOCATION.search(text)
            if m:
                event["location"] = m.group(1).strip()

        logger.info(
            "[C10]   %d PDFs, date='%s', location='%s'",
            len(pdf_urls), event["date_range"][:50], event["location"][:50],
        )

    # -----------------------------------------------------------------------
    # PDF extraction
    # -----------------------------------------------------------------------

    def _download_pdf_pages(self, pdf_url: str) -> List[str]:
        """Download a PDF and extract text from each page."""
        logger.info("[C10] Downloading PDF: %s", pdf_url)

        content = self._get_bytes(pdf_url)
        if not content:
            return []

        raw = BytesIO(content)
        pages = []

        if PDF_ENGINE == "pdfplumber":
            import pdfplumber
            try:
                with pdfplumber.open(raw) as pdf:
                    for page in pdf.pages:
                        text = page.extract_text() or ""
                        pages.append(text)
            except Exception as exc:
                logger.error("[C10] pdfplumber error: %s", exc)

        elif PDF_ENGINE == "pymupdf":
            import fitz
            try:
                doc = fitz.open(stream=raw, filetype="pdf")
                for page in doc:
                    pages.append(page.get_text())
                doc.close()
            except Exception as exc:
                logger.error("[C10] PyMuPDF error: %s", exc)

        else:
            logger.error("[C10] No PDF library available. Install pdfplumber or PyMuPDF.")
            return []

        logger.info("[C10]   Extracted %d pages of text", len(pages))
        return pages

    # -----------------------------------------------------------------------
    # PDF parsing: extract case records
    # -----------------------------------------------------------------------

    def _parse_pdf_cases(
        self,
        pages: List[str],
        event: Dict,
        pdf_url: str,
    ) -> List[Dict]:
        """
        Parse case information from PDF text pages.

        Tracks context as it walks through lines:
          - current_date (from date headers)
          - current_time (from session headers)
          - current_courtroom (from courtroom headers)
          - current_judges (from "Before:" lines)

        Returns raw case dicts.
        """
        full_text = "\n".join(pages)
        self.raw_data.append({"event": event, "text": full_text, "pdf_url": pdf_url})

        lines = full_text.split("\n")
        cases = []

        current_date = ""
        current_time = ""
        current_courtroom = ""
        current_judges = ""

        for idx, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue

            # --- Context: Date header ---
            date_m = C10_RE_DATE_FULL.search(stripped)
            if date_m:
                current_date = date_m.group(1)
                continue

            # If no full date found yet, try short date
            if not current_date:
                date_m = C10_RE_DATE_SHORT.search(stripped)
                if date_m and len(stripped) < 60:
                    current_date = date_m.group(1)
                    continue

            # --- Context: Courtroom ---
            cr_m = C10_RE_COURTROOM.search(stripped)
            if cr_m and len(stripped) < 80:
                current_courtroom = f"Courtroom {cr_m.group(1)}"
                # Check if time is on the same line
                time_m = C10_RE_TIME.search(stripped)
                if time_m:
                    current_time = self._normalize_time(time_m.group(1))
                continue

            # --- Context: Session time ---
            sess_m = C10_RE_SESSION.search(stripped)
            if sess_m:
                current_time = self._normalize_time(sess_m.group(1))
                continue

            # Standalone time line (e.g., "9:00 a.m.")
            time_m = C10_RE_TIME.match(stripped)
            if time_m and len(stripped) < 30:
                current_time = self._normalize_time(time_m.group(1))
                continue

            # --- Context: "Before:" judges ---
            if stripped.lower().startswith("before"):
                judges = self._extract_judges(stripped, lines, idx)
                if judges:
                    current_judges = judges
                continue

            # --- Data: Case number line ---
            case_nums = C10_RE_CASE_NUM.findall(stripped)
            if not case_nums:
                continue

            for case_num in case_nums:
                case_name = self._extract_case_name(stripped, case_num, lines, idx)
                is_submitted = bool(C10_RE_SUBMIT.search(stripped))

                # Check surrounding lines for submit marker too
                if not is_submitted:
                    for offset in range(-2, 3):
                        check_idx = idx + offset
                        if 0 <= check_idx < len(lines) and check_idx != idx:
                            context_line = lines[check_idx].strip()
                            if (C10_RE_SUBMIT.search(context_line) and
                                    len(context_line) < 40):
                                is_submitted = True
                                break

                # Extract time allotment
                time_allot = ""
                allot_m = C10_RE_TIME_ALLOT.search(stripped)
                if allot_m:
                    time_allot = f"{allot_m.group(1)} min"

                purpose = "Submitted on Briefs" if is_submitted else "Oral Argument"

                # Build description
                desc_parts = []
                if time_allot:
                    desc_parts.append(f"Argument time: {time_allot}")
                if is_submitted:
                    desc_parts.append("Submitted on the briefs")

                cases.append({
                    "case_number": case_num,
                    "case_name": case_name,
                    "date": current_date,
                    "time": current_time,
                    "courtroom": current_courtroom,
                    "judges": current_judges,
                    "location": event.get("location", ""),
                    "purpose": purpose,
                    "description": "; ".join(desc_parts),
                    "pdf_url": pdf_url,
                    "event_title": event.get("title", ""),
                })

        # Deduplicate by case number within this PDF
        seen = set()
        unique = []
        for c in cases:
            key = (c["case_number"], c["date"])
            if key not in seen:
                seen.add(key)
                unique.append(c)

        logger.info("[C10]   Parsed %d unique cases from PDF", len(unique))
        return unique

    def _extract_case_name(
        self,
        line: str,
        case_num: str,
        lines: List[str],
        idx: int,
    ) -> str:
        """Extract case name from the line containing the case number."""
        # Get text after the case number
        parts = line.split(case_num, 1)
        after = parts[-1].strip(" :-–—\t") if len(parts) > 1 else ""

        # Remove time allotment from end
        after = C10_RE_TIME_ALLOT.sub("", after).strip(" :-–—\t")

        # Remove submit markers
        after = C10_RE_SUBMIT.sub("", after).strip(" :-–—\t")

        case_name = after.strip()

        # If no name found, check next line for "v." pattern
        if not case_name and idx + 1 < len(lines):
            next_line = lines[idx + 1].strip()
            if C10_RE_VS.search(next_line) and not C10_RE_CASE_NUM.search(next_line):
                case_name = next_line.strip()

        # If still no name but current line has "v.", extract around it
        if not case_name and " v. " in line.lower():
            case_name = after.strip()

        # Clean up
        case_name = re.sub(r"\s{2,}", " ", case_name)
        case_name = case_name.strip(" .,;:-–—")

        return case_name[:300]  # Reasonable limit

    def _extract_judges(
        self,
        line: str,
        lines: List[str],
        idx: int,
    ) -> str:
        """Extract judge names from a 'Before:' line and continuation lines."""
        # Collect the full "Before:" block (may span multiple lines)
        block = line
        for offset in range(1, 5):
            check_idx = idx + offset
            if check_idx >= len(lines):
                break
            next_line = lines[check_idx].strip()
            if not next_line:
                break
            # Stop if we hit a case number or another section
            if C10_RE_CASE_NUM.search(next_line):
                break
            if next_line.lower().startswith(("courtroom", "before")):
                break
            block += " " + next_line
            # Stop if we see "Judges" or "Judge" at end
            if re.search(r"Judges?\.?\s*$", next_line, re.IGNORECASE):
                break

        # Extract names
        m = C10_RE_BEFORE.search(block)
        if m:
            raw = m.group(1).strip()
        else:
            raw = re.sub(r"^Before[:\s]+", "", block, flags=re.IGNORECASE).strip()

        # Clean up
        raw = re.sub(
            r",?\s*(?:and\s+)?(?:Senior\s+)?(?:Circuit\s+)?(?:District\s+)?Judges?\.?\s*$",
            "", raw, flags=re.IGNORECASE,
        ).strip()
        raw = re.sub(
            r",?\s*Chief\s+(?:Circuit\s+)?Judge\.?\s*$",
            "", raw, flags=re.IGNORECASE,
        ).strip()
        raw = re.sub(r"\s*,?\s+and\s+", ", ", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s{2,}", " ", raw)

        return raw.strip(" ,.")

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
        m = C10_RE_DATE_SHORT.search(raw_date)
        if m:
            for fmt in ("%B %d, %Y", "%B %d %Y"):
                try:
                    return datetime.strptime(m.group(1), fmt).strftime("%Y-%m-%d")
                except ValueError:
                    continue

        return raw_date

    @staticmethod
    def _normalize_time(raw_time: str) -> str:
        """Normalize time to consistent format."""
        if not raw_time:
            return ""
        raw_time = raw_time.strip()

        # Normalize a.m./p.m. to AM/PM
        normalized = re.sub(r"[Aa]\.?[Mm]\.?", "AM", raw_time)
        normalized = re.sub(r"[Pp]\.?[Mm]\.?", "PM", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()

        for fmt in ("%I:%M %p", "%I:%M%p"):
            try:
                parsed = datetime.strptime(normalized, fmt)
                return parsed.strftime("%-I:%M %p")
            except ValueError:
                continue

        return raw_time

    @staticmethod
    def _normalize_location(raw_location: str) -> str:
        """Normalize location string."""
        if not raw_location:
            return C10_DEFAULT_LOCATION

        loc = raw_location.strip()
        lower = loc.lower()

        if "denver" in lower or "byron white" in lower:
            return C10_DEFAULT_LOCATION
        if "oklahoma" in lower:
            return "Oklahoma City, OK"
        if "wichita" in lower:
            return "Wichita, KS"
        if "albuquerque" in lower:
            return "Albuquerque, NM"
        if "salt lake" in lower:
            return "Salt Lake City, UT"
        if "cheyenne" in lower:
            return "Cheyenne, WY"

        return loc if loc else C10_DEFAULT_LOCATION

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
            location = self._normalize_location(r.get("location", ""))

            normalized.append({
                "Case Name":          r.get("case_name", "").strip(),
                "Case Number":        r.get("case_number", "").strip(),
                "Nature of Case":     "",  # Not reliably available in PDFs
                "Court Name":         C10_COURT_NAME,
                "Location":           location,
                "Judges / Panel":     r.get("judges", "").strip(),
                "Courtroom":          r.get("courtroom", "").strip(),
                "Purpose of Hearing": r.get("purpose", "Oral Argument"),
                "Date":               self._format_date(r.get("date", "")),
                "Time":               r.get("time", "").strip(),
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
        Main entry point. Scrapes all Tenth Circuit oral argument calendars.

        Returns a list of dicts in the standard 11-field schema.

        progress_callback signature: (current: int, total: int, label: str)
        """
        return self.scrape_all(progress_callback)

    def scrape_all(
        self,
        progress_callback: Optional[Callable] = None,
    ) -> List[Dict]:
        """Scrape all available calendar events and their PDFs."""
        if PDF_ENGINE is None:
            logger.error("[C10] No PDF library available. Install pdfplumber or PyMuPDF.")
            return []

        # Step 1: Discover events
        if progress_callback:
            progress_callback(0, 1, "Discovering calendar events...")

        events = self.discover_events()
        if not events:
            logger.warning("[C10] No calendar events found")
            if progress_callback:
                progress_callback(1, 1, "No events found")
            return []

        # Count total PDFs
        total_pdfs = sum(len(e.get("pdf_urls", [])) for e in events)
        logger.info("[C10] Found %d events with %d total PDFs", len(events), total_pdfs)

        if progress_callback:
            progress_callback(
                1, max(total_pdfs, 1),
                f"Found {len(events)} events, {total_pdfs} PDFs",
            )

        # Step 2: Download and parse each PDF
        all_raw = []
        pdf_count = 0

        for event in events:
            for pdf_url in event.get("pdf_urls", []):
                pdf_count += 1

                if progress_callback:
                    label = event.get("title", pdf_url.split("/")[-1])[:60]
                    progress_callback(pdf_count, total_pdfs, label)

                pages = self._download_pdf_pages(pdf_url)
                if pages:
                    cases = self._parse_pdf_cases(pages, event, pdf_url)
                    all_raw.extend(cases)
                    logger.info(
                        "[C10] PDF %d/%d: %d cases from %s",
                        pdf_count, total_pdfs, len(cases),
                        event.get("title", "")[:50],
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

        logger.info("[C10] Final: %d cases (%d before dedup)",
                    len(unique), len(normalized))

        if progress_callback:
            progress_callback(total_pdfs, total_pdfs, f"Complete — {len(unique)} cases")

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

    scraper = USCA10Scraper()

    print("=" * 70)
    print("TENTH CIRCUIT SCRAPER — STANDARDIZED")
    print("=" * 70)

    if PDF_ENGINE:
        print(f"PDF engine: {PDF_ENGINE}")
    else:
        print("ERROR: No PDF library found. Install pdfplumber or PyMuPDF.")
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