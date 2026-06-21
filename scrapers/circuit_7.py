"""
Seventh Circuit Court of Appeals — Oral Argument Calendar Scraper.
Downloads and parses the week/session calendar PDF from ca7.uscourts.gov.

The PDF contains two types of content:
  - Session schedule pages: date ranges for potential argument weeks (no case data)
  - Weekly argument pages: individual cases with case number, name, time, duration

Only argument pages produce output records. Session schedule pages are skipped
as they contain no case-level data.
"""

import re
import io
import time
from datetime import datetime
from typing import List, Dict, Optional

from scrapers.base import _make_ssl_session
from utils.constants import DEFAULT_C7_SESSION_PDF
from utils.helpers import HAS_PDFPLUMBER, HAS_PYPDF2, logger

if HAS_PDFPLUMBER:
    import pdfplumber
if HAS_PYPDF2:
    from PyPDF2 import PdfReader

# ── Constants ──
C7_COURT_NAME = "United States Court of Appeals for the Seventh Circuit"
C7_DEFAULT_LOCATION = "Chicago, IL"

# ── Compiled patterns ──

# Full date header: "Wednesday, June 3, 2026"
C7_RE_DATE_HEADER = re.compile(
    r"((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
    r",\s+(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{1,2},\s+\d{4})",
    re.IGNORECASE,
)

# Case entry: "25-1936; USA v. Nirav Patel" (semicolon-delimited)
C7_RE_CASE_ENTRY = re.compile(
    r"(\d{2}-\d{4})\s*;\s*(.+?)(?=\n|$)"
)

# Case number standalone
C7_RE_CASE_NUM = re.compile(r"\b(\d{2}-\d{4})\b")

# Time: "9:30 a.m.", "4:00 PM", "2:00 p.m.", "10:30 AM"
C7_RE_TIME = re.compile(
    r"\b(\d{1,2}:\d{2}\s*(?:[AaPp]\.?[Mm]\.?))\b"
)

# Duration: "10 min", "15 min", "20 MINUTES"
C7_RE_DURATION = re.compile(
    r"\b(\d+)\s*(?:min(?:utes?)?)\b", re.IGNORECASE
)

# Submitted on briefs
C7_RE_ON_BRIEFS = re.compile(
    r"SUBMITTED\s+ON\s+BRIEFS", re.IGNORECASE
)

# Session header: "APRIL, 2026 SESSION"
C7_RE_SESSION_HEADER = re.compile(
    r"([A-Z]+,\s*\d{4})\s+SESSION", re.IGNORECASE
)

# Argument type: "Oral Argument" or "Video Argument"
C7_RE_ARG_TYPE = re.compile(
    r"\b(Video\s+Argument|Oral\s+Argument)\b", re.IGNORECASE
)

# "Short Argument Days" or "START" marker
C7_RE_SHORT_ARG = re.compile(r"Short\s+Argument\s+Days?", re.IGNORECASE)
C7_RE_START_MARKER = re.compile(
    r"\d{1,2}:\d{2}\s*[AaPp]\.?[Mm]\.?\s*START", re.IGNORECASE
)

# Page-level date (less strict): "Monday, June 15"
C7_RE_PAGE_DATE_SHORT = re.compile(
    r"((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
    r",\s+(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{1,2})",
    re.IGNORECASE,
)


class USCA7Scraper:
    """Scraper for the Seventh Circuit Court of Appeals oral argument calendars."""

    PDFS = {
        "week_session": {
            "url": DEFAULT_C7_SESSION_PDF,
            "filename": "week-session-calendar.pdf",
            "description": "Week and Session Calendar",
        },
    }

    def __init__(self, verify_ssl: bool = False, **kwargs):
        self.verify_ssl = verify_ssl
        self.session = _make_ssl_session()
        self._raw_data = None

    # ── Date formatting ──

    @staticmethod
    def _format_date(raw_date: str) -> str:
        """
        Convert long-form dates to YYYY-MM-DD.
          'Wednesday, June 3, 2026' → '2026-06-03'
          'June 3, 2026'            → '2026-06-03'
        Returns original string if parsing fails.
        """
        if not raw_date:
            return ""
        # Remove day-of-week prefix
        cleaned = re.sub(
            r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
            r"[,\s]+", "", raw_date, flags=re.IGNORECASE
        ).strip()
        for fmt in ("%B %d, %Y", "%B %d %Y"):
            try:
                return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return raw_date

    # ── Download ──

    def download_pdf(self, url: str) -> Optional[bytes]:
        """Download a PDF and return raw bytes, or None on failure."""
        try:
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
            size_kb = len(resp.content) / 1024
            logger.info(
                f"[C7] Downloaded {url.rsplit('/', 1)[-1]} ({size_kb:.1f} KB)"
            )
            return resp.content
        except Exception as e:
            logger.error(f"[C7] Failed to download {url}: {e}")
            return None

    # ── Text / table extraction (per-page) ──

    @staticmethod
    def _extract_text_by_page(pdf_bytes: bytes) -> List[str]:
        """Extract text from each page separately."""
        pages = []
        if HAS_PDFPLUMBER:
            try:
                with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                    for page in pdf.pages:
                        text = page.extract_text()
                        pages.append(text.strip() if text else "")
            except Exception as e:
                logger.warning(f"[C7] pdfplumber failed: {e}")
        elif HAS_PYPDF2:
            try:
                reader = PdfReader(io.BytesIO(pdf_bytes))
                for page in reader.pages:
                    try:
                        txt = page.extract_text() or ""
                    except Exception:
                        txt = ""
                    pages.append(txt.strip())
            except Exception as e:
                logger.warning(f"[C7] PyPDF2 failed: {e}")
        return pages

    @staticmethod
    def _extract_tables_by_page(pdf_bytes: bytes) -> List[List]:
        """Extract tables from each page separately."""
        all_page_tables = []
        if HAS_PDFPLUMBER:
            try:
                with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                    for page in pdf.pages:
                        tables = page.extract_tables() or []
                        all_page_tables.append(tables)
            except Exception as e:
                logger.warning(f"[C7] Table extraction failed: {e}")
        return all_page_tables

    # ── Page classification ──

    @staticmethod
    def _is_session_schedule_page(page_text: str) -> bool:
        """
        Returns True if the page is a session schedule page
        (date ranges + day counts, no individual cases).
        """
        if C7_RE_SESSION_HEADER.search(page_text):
            if not C7_RE_CASE_ENTRY.search(page_text):
                return True
        return False

    @staticmethod
    def _is_argument_page(page_text: str) -> bool:
        """Returns True if the page contains individual case argument entries."""
        return bool(C7_RE_CASE_ENTRY.search(page_text))

    # ── Case name cleaning ──

    @staticmethod
    def _clean_case_name(raw_name: str) -> str:
        """
        Clean a raw case name string:
        - Remove trailing duration info ("10 min", "15 MINUTES")
        - Remove "SUBMITTED ON BRIEFS"
        - Remove START markers ("9:30 a.m. START")
        - Remove trailing commas and whitespace
        """
        name = raw_name.strip()
        # Remove "SUBMITTED ON BRIEFS"
        name = C7_RE_ON_BRIEFS.sub("", name).strip()
        # Remove trailing duration
        name = re.sub(
            r"\s*\d+\s*(?:min(?:utes?)?)\s*$", "",
            name, flags=re.IGNORECASE
        ).strip()
        # Remove START markers
        name = C7_RE_START_MARKER.sub("", name).strip()
        # Remove trailing time patterns
        name = re.sub(
            r"\s*\d{1,2}:\d{2}\s*[AaPp]\.?[Mm]\.?\s*$", "", name
        ).strip()
        # Remove trailing commas
        name = name.rstrip(",").strip()
        return name

    # ── Argument page parsing ──

    def _parse_argument_page(
        self, page_text: str, page_tables: List = None
    ) -> List[Dict]:
        """
        Parse an argument page into individual case records.
        Uses line-based parsing as primary strategy since table extraction
        tends to split case names at cell boundaries.
        """
        records = []

        # ── Get the page-level date ──
        page_date = ""
        date_m = C7_RE_DATE_HEADER.search(page_text)
        if date_m:
            page_date = date_m.group(1).strip()
        else:
            date_m2 = C7_RE_PAGE_DATE_SHORT.search(page_text)
            if date_m2:
                year_m = re.search(r"\b(20\d{2})\b", page_text)
                if year_m:
                    page_date = f"{date_m2.group(1)}, {year_m.group(1)}"
                else:
                    page_date = date_m2.group(1)

        # ── Line-based parsing ──
        lines = page_text.split("\n")
        current_time = ""

        i = 0
        while i < len(lines):
            line = lines[i].strip()
            i += 1

            if not line:
                continue

            # Skip header/footer lines
            if re.match(
                r"^(UNITED STATES|SEVENTH CIRCUIT|U\.S\.\s*COURT|Page\s)",
                line, re.IGNORECASE
            ):
                continue

            # Track current time — standalone time line or time with START
            time_only = C7_RE_TIME.search(line)
            if time_only and not C7_RE_CASE_ENTRY.search(line):
                current_time = time_only.group(1).strip()
                continue

            # ── Case entry line ──
            case_m = C7_RE_CASE_ENTRY.search(line)
            if not case_m:
                continue

            case_num = case_m.group(1)
            case_name_raw = case_m.group(2).strip()

            # Check if time appears before the case number on this line
            pre_case = line[:case_m.start()].strip()
            time_on_line = C7_RE_TIME.search(pre_case)
            if time_on_line:
                current_time = time_on_line.group(1).strip()

            # ── Gather continuation lines for multi-line case names ──
            # Keep reading lines until we hit a terminator
            full_name_parts = [case_name_raw]

            while i < len(lines):
                next_line = lines[i].strip()

                # Empty line → end of this case block
                if not next_line:
                    break

                # Next case entry → stop (don't consume)
                if C7_RE_CASE_ENTRY.search(next_line):
                    break

                # Standalone time → stop (don't consume)
                if (C7_RE_TIME.search(next_line)
                        and not C7_RE_CASE_ENTRY.search(next_line)
                        and not re.search(r"[a-zA-Z]{3,}", next_line)):
                    break

                # Date header → stop
                if C7_RE_DATE_HEADER.search(next_line):
                    break

                # Duration line → extract and stop
                if C7_RE_DURATION.match(next_line):
                    break

                # "SUBMITTED ON BRIEFS" → stop
                if C7_RE_ON_BRIEFS.search(next_line):
                    break

                # START marker line → stop
                if C7_RE_START_MARKER.search(next_line):
                    break

                # Short argument marker → stop
                if C7_RE_SHORT_ARG.search(next_line):
                    break

                # Otherwise it's a continuation of the case name
                full_name_parts.append(next_line)
                i += 1

            # ── Assemble and clean the full case name ──
            full_name = " ".join(full_name_parts)
            case_name = self._clean_case_name(full_name)

            # ── Extract duration from the case name or following lines ──
            time_allotted = ""

            # Check within the raw assembled name first
            if C7_RE_ON_BRIEFS.search(full_name):
                time_allotted = "Submitted On Briefs"
            else:
                dur_m = C7_RE_DURATION.search(full_name)
                if dur_m:
                    time_allotted = f"{dur_m.group(1)} Minutes"

            # If not found, look at the next non-empty line
            if not time_allotted:
                peek = i
                while peek < len(lines) and peek < i + 3:
                    peek_line = lines[peek].strip()
                    if not peek_line:
                        peek += 1
                        continue
                    if C7_RE_ON_BRIEFS.search(peek_line):
                        time_allotted = "Submitted On Briefs"
                        break
                    dur_m = C7_RE_DURATION.search(peek_line)
                    if dur_m:
                        time_allotted = f"{dur_m.group(1)} Minutes"
                        break
                    # If it's a new case or time, stop looking
                    if (C7_RE_CASE_ENTRY.search(peek_line)
                            or C7_RE_DATE_HEADER.search(peek_line)):
                        break
                    peek += 1

            records.append({
                "case_numbers": [case_num],
                "case_name": case_name,
                "argument_date": page_date,
                "argument_time": current_time,
                "judges": "",
                "courtroom": "",
                "time_allotted": time_allotted,
                "description": "",
                "record_type": "argument",
            })

        return records

    # ── Full PDF parsing ──

    def parse_pdf(self, pdf_bytes: bytes, pdf_url: str) -> List[Dict]:
        """
        Parse the complete PDF page-by-page.
        Only argument pages produce records; session schedule pages are skipped.
        """
        pages_text = self._extract_text_by_page(pdf_bytes)
        pages_tables = self._extract_tables_by_page(pdf_bytes)

        if not pages_text:
            logger.warning(f"[C7] No text extracted from {pdf_url}")
            return []

        all_records = []
        session_pages = 0
        argument_pages = 0

        for page_idx, page_text in enumerate(pages_text):
            if not page_text:
                continue

            page_tables = (
                pages_tables[page_idx]
                if page_idx < len(pages_tables)
                else []
            )

            if self._is_session_schedule_page(page_text):
                session_pages += 1
                logger.debug(
                    f"[C7] Page {page_idx + 1}: session schedule (skipped)"
                )
                continue

            if self._is_argument_page(page_text):
                argument_pages += 1
                records = self._parse_argument_page(page_text, page_tables)
                logger.debug(
                    f"[C7] Page {page_idx + 1}: argument page "
                    f"({len(records)} cases)"
                )
                for rec in records:
                    rec["pdf_url"] = pdf_url
                    rec["source_page"] = page_idx + 1
                all_records.extend(records)
            else:
                logger.debug(
                    f"[C7] Page {page_idx + 1}: unrecognized format, skipping"
                )

        logger.info(
            f"[C7] PDF summary: {argument_pages} argument page(s), "
            f"{session_pages} session schedule page(s) skipped, "
            f"{len(all_records)} case(s) extracted"
        )
        return all_records

    # ── Normalization to standard schema ──

    @staticmethod
    def normalize_cases(raw_cases: List[Dict]) -> List[Dict]:
        """
        Map raw parsed records to the standard 11-field output schema.
        Only argument records are included (session schedules are already
        filtered out during parsing).
        """
        normalized = []
        for c in raw_cases:
            case_nums = c.get("case_numbers", [])

            normalized.append({
                "Date": USCA7Scraper._format_date(
                    c.get("argument_date", "")
                ),
                "Case Number": ", ".join(case_nums) if case_nums else "",
                "Case Name": c.get("case_name", ""),
                "Nature of Case": "",
                "Court Name": C7_COURT_NAME,
                "Location": C7_DEFAULT_LOCATION,
                "Judges / Panel": c.get("judges", ""),
                "Courtroom": c.get("courtroom", ""),
                "Purpose of Hearing": "Oral Argument",
                "Time": c.get("argument_time", ""),
                "Description": c.get("time_allotted", ""),
            })
        return normalized

    # ── Public interface ──

    def scrape_all(self, progress_callback=None) -> List[Dict]:
        """
        Main entry point. Downloads the session PDF, parses, normalizes.
        Only returns case-level argument records.
        """
        pdf_info = self.PDFS["week_session"]
        url = pdf_info["url"]
        description = pdf_info["description"]

        if progress_callback:
            progress_callback(1, 1, description)

        pdf_bytes = self.download_pdf(url)
        if pdf_bytes is None:
            logger.warning(f"[C7] Failed to download {description}")
            return []

        all_raw = self.parse_pdf(pdf_bytes, url)
        self._raw_data = all_raw

        logger.info(f"[C7] Parsed {len(all_raw)} case(s) from {description}")

        normalized = self.normalize_cases(all_raw)
        logger.info(f"[C7] Total: {len(normalized)} normalized records")
        return normalized

    def get_raw_data(self):
        """Return the raw parsed data (before normalization)."""
        return self._raw_data

    def get_raw_texts(self) -> Dict[str, str]:
        """Download and return raw text for debugging."""
        texts = {}
        for key, pdf_info in self.PDFS.items():
            pdf_bytes = self.download_pdf(pdf_info["url"])
            if pdf_bytes:
                pages = self._extract_text_by_page(pdf_bytes)
                texts[pdf_info["description"]] = (
                    "\n\n--- PAGE BREAK ---\n\n".join(pages)
                )
            else:
                texts[pdf_info["description"]] = "(download failed)"
        return texts