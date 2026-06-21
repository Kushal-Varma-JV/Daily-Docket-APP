"""
Sixth Circuit Court of Appeals — Oral Argument Calendar Scraper.
Discovers and parses PDF calendars from ca6.uscourts.gov.

Architecture:
  1. discover_pdf_links()       — crawl calendar page for PDF links
  2. download_pdf()             — download each PDF
  3. _extract_text_by_page()    — extract text per page (not whole PDF)
  4. _parse_sessions()          — split text into per-session sections
  5. _parse_case_block()        — extract structured fields per case
  6. normalize_cases()          — map to standard 11-field output schema
  7. scrape_all()               — orchestrates everything
"""

import re
import io
import time
from datetime import datetime
import pandas as pd
from typing import List, Dict, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from scrapers.base import _make_ssl_session
from utils.constants import DEFAULT_C6_URL
from utils.helpers import HAS_PDFPLUMBER, HAS_PYPDF2, logger

if HAS_PDFPLUMBER:
    import pdfplumber
if HAS_PYPDF2:
    from PyPDF2 import PdfReader

# ── Constants ──
C6_COURT_NAME = "United States Court of Appeals for the Sixth Circuit"
C6_DEFAULT_LOCATION = "Cincinnati, OH"
C6_REQUIRED_PATH_FRAGMENT = "oral_argument_calendars/"
C6_EXCLUDE_KEYWORDS = [
    "master sitting", "sitting schedule", "annual",
    "holiday", "public master",
]

# ── Compiled regex patterns ──

# Session header: "Tuesday, June 2, 2026"
C6_RE_DATE_LONG = re.compile(
    r"((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
    r"[,\s]+(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{1,2}[,\s]+\d{4})",
    re.IGNORECASE,
)

# Courtroom + Time: "403 - 4th Floor Courtroom, 9:00 A.M."
# Also handles: "636 - 6th Floor East Courtroom, 10:00 A.M."
C6_RE_COURTROOM_TIME = re.compile(
    r"(\d{3}\s*-\s*\d+\w*\s+Floor\s+[\w\s]+Courtroom)"
    r"[,\s]+([\d:]+\s*[AP]\.?M\.?)",
    re.IGNORECASE,
)

# Fallback time pattern if courtroom line doesn't match
C6_RE_TIME_STANDALONE = re.compile(
    r"\b(\d{1,2}:\d{2}\s*[AP]\.?M\.?)\b", re.IGNORECASE
)

# "Before: Moore, White, Thapar"
C6_RE_BEFORE = re.compile(r"Before:\s*(.+)", re.IGNORECASE)

# "EnBanc" (no space, as seen in actual PDF)
C6_RE_EN_BANC = re.compile(r"\bEn\s*Banc\b", re.IGNORECASE)

# Case number: "24-2062" or consolidated "25-5738/25-5739"
C6_RE_CASE_NUM = re.compile(r"\b(\d{2}-\d{3,5})\b")

# Lower court case number: "1:22-cr-20187-1" style
C6_RE_LOWER_COURT_CASE = re.compile(r"\b(\d:\d{2}-\w{2}-\d{4,6}(?:-\d+)?)\b")

# Immigration case number: "a 212 951 791" style
C6_RE_IMMIGRATION_CASE = re.compile(r"\b([aA]\s+\d{3}\s+\d{3}\s+\d{3})\b")

# Time allotted — multiple formats
C6_RE_TIME_ALLOTTED_SIMPLE = re.compile(
    r"\((\d+)\s*[Mm]inutes?\s*[Pp]er\s*[Ss]ide\)"
)
C6_RE_TIME_ALLOTTED_FULL = re.compile(
    r"\(([^)]*[Mm]inutes?[^)]*)\)"
)
C6_RE_ON_BRIEFS = re.compile(
    r"\(To\s+Be\s+Submitted\s+On\s+Briefs\)", re.IGNORECASE
)

# Argument type: "Oral Argument" or "Video Argument"
C6_RE_ARG_TYPE = re.compile(
    r"\b(Video\s+Argument|Oral\s+Argument)\b", re.IGNORECASE
)

# Posted/Revised stamp
C6_RE_POSTED = re.compile(
    r"\*?\*?Posted\s+on\s+(\d{1,2}/\d{1,2}/\d{2,4})\s+at\s+([\d:]+\s*[ap]\.?m\.?)\*?\*?",
    re.IGNORECASE,
)
C6_RE_REVISED = re.compile(
    r"REVISED\s+([\d:]+\s*[ap]\.?m\.?)[,\s]+([\w\s]+\d{1,2}[,\s]+\d{4})",
    re.IGNORECASE,
)

# Session header detection — a line that contains a long date AND
# is followed within a few lines by "Before:" or "EnBanc"
C6_RE_SESSION_SPLIT = re.compile(
    r"(?=(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
    r"[,\s]+(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{1,2}[,\s]+\d{4})",
    re.IGNORECASE,
)


class USCA6Scraper:
    """Scraper for the Sixth Circuit Court of Appeals oral argument calendars."""

    def __init__(self, verify_ssl: bool = False, delay: float = 0.3, **kwargs):
        self.base_url = kwargs.get("base_url", DEFAULT_C6_URL)
        self.verify_ssl = verify_ssl
        self.delay = delay
        self.court_name = C6_COURT_NAME
        self._raw_data = None
        self.session = _make_ssl_session()

    # ── Discovery ──

    def discover_pdf_links(self) -> List[Dict]:
        """Fetch the calendar page and return a list of PDF link dicts."""
        logger.info("[C6] Fetching calendar page …")
        resp = self.session.get(self.base_url, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        pdfs = []
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"].strip()
            if not href.lower().endswith(".pdf"):
                continue
            abs_url = urljoin(self.base_url, href)
            if C6_REQUIRED_PATH_FRAGMENT not in abs_url:
                continue
            link_text = a_tag.get_text(strip=True)
            combined = f"{link_text} {href}".lower()
            if any(kw in combined for kw in C6_EXCLUDE_KEYWORDS):
                continue
            pdfs.append({"url": abs_url, "link_text": link_text})
        logger.info(f"[C6] Discovered {len(pdfs)} case calendar PDF(s).")
        return pdfs

    # ── Download ──

    def download_pdf(self, url: str) -> Optional[bytes]:
        """Download a PDF and return raw bytes, or None on failure."""
        try:
            resp = self.session.get(url, timeout=60)
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            logger.error(f"[C6] Failed to download {url}: {e}")
            return None

    # ── Text extraction (per-page) ──

    @staticmethod
    def _extract_text_by_page(pdf_bytes: bytes) -> List[str]:
        """
        Extract text from each page of the PDF separately.
        Returns a list of strings, one per page.
        """
        pages = []
        if HAS_PDFPLUMBER:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    pages.append(text.strip() if text else "")
        elif HAS_PYPDF2:
            reader = PdfReader(io.BytesIO(pdf_bytes))
            for page in reader.pages:
                try:
                    txt = page.extract_text() or ""
                except Exception:
                    txt = ""
                pages.append(txt.strip())
        return pages

    @staticmethod
    def _extract_full_text(pdf_bytes: bytes) -> str:
        """Extract all text from the PDF as a single string."""
        pages = USCA6Scraper._extract_text_by_page(pdf_bytes)
        return "\n\n".join(pages)

    # ── Date formatting ──

    @staticmethod
    def _format_date(raw_date: str) -> str:
        """Convert 'Tuesday, June 2, 2026' → '2026-06-02'."""
        if not raw_date:
            return ""
        # Remove day-of-week prefix if present
        cleaned = re.sub(
            r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
            r"[,\s]+", "", raw_date, flags=re.IGNORECASE
        ).strip()
        for fmt in ("%B %d, %Y", "%B %d %Y"):
            try:
                return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return raw_date  # Return original if parsing fails

    # ── Session parsing ──

    @staticmethod
    def _parse_session_header(text: str) -> Dict:
        """
        Parse the header portion of a session section to extract:
        date, courtroom, time, judges, argument type, en_banc flag.
        """
        header = {
            "argument_date": "",
            "courtroom": "",
            "argument_time": "",
            "panel_judges": [],
            "argument_type": "Oral Argument",
            "en_banc": False,
        }

        # Date
        date_m = C6_RE_DATE_LONG.search(text)
        if date_m:
            header["argument_date"] = date_m.group(0).strip()

        # Courtroom + Time (combined pattern)
        ct_m = C6_RE_COURTROOM_TIME.search(text)
        if ct_m:
            header["courtroom"] = ct_m.group(1).strip()
            header["argument_time"] = ct_m.group(2).strip()
        else:
            # Fallback: try standalone time
            time_m = C6_RE_TIME_STANDALONE.search(text)
            if time_m:
                header["argument_time"] = time_m.group(1).strip()

        # Argument type
        arg_m = C6_RE_ARG_TYPE.search(text)
        if arg_m:
            header["argument_type"] = arg_m.group(1).strip()

        # En Banc
        if C6_RE_EN_BANC.search(text):
            header["en_banc"] = True
            header["panel_judges"] = ["En Banc"]

        # Panel judges (only if not en banc)
        if not header["en_banc"]:
            before_m = C6_RE_BEFORE.search(text)
            if before_m:
                raw_judges = before_m.group(1)
                judges = [j.strip() for j in raw_judges.split(",") if j.strip()]
                # Filter out non-judge text that might follow
                clean_judges = []
                for j in judges:
                    if C6_RE_CASE_NUM.search(j):
                        break
                    clean_judges.append(j)
                header["panel_judges"] = clean_judges

        return header

    @staticmethod
    def _split_into_sessions(full_text: str) -> List[str]:
        """
        Split the full PDF text into session sections.
        Each session starts with a long date line (e.g., "Tuesday, June 2, 2026").
        """
        sections = C6_RE_SESSION_SPLIT.split(full_text)
        sessions = [s.strip() for s in sections if s.strip()]
        if len(sessions) <= 1:
            return [full_text]
        return sessions

    @staticmethod
    def _split_into_case_blocks(session_text: str) -> List[str]:
        """
        Within a session, split into individual case blocks.
        Each case starts with a case number at the beginning of a line.
        Handles consolidated cases (e.g., "25-5738/25-5739").
        """
        splitter = re.compile(
            r"(?=^\d{2}-\d{3,5}(?:/\d{2}-\d{3,5})*\s)", re.MULTILINE
        )
        blocks = [b.strip() for b in splitter.split(session_text) if b.strip()]
        case_blocks = []
        for block in blocks:
            if C6_RE_CASE_NUM.match(block):
                case_blocks.append(block)
        return case_blocks

    # ── Case block parsing ──

    @staticmethod
    def _parse_case_block(block: str, session_meta: Dict) -> Dict:
        """
        Parse a single case block and merge with session metadata.
        Returns a dict with all extracted fields.
        """
        rec = dict(session_meta)
        rec["raw_text"] = block

        # ── Case numbers (may be consolidated: "25-5738/25-5739") ──
        first_line = block.split("\n")[0]
        rec["case_numbers"] = C6_RE_CASE_NUM.findall(first_line)

        # ── Case name: look for "v." pattern ──
        rec["case_name"] = ""
        for line in block.split("\n"):
            if " v. " in line:
                name = re.sub(
                    r"^\d{2}-\d{3,5}(/\d{2}-\d{3,5})*\s*", "", line
                ).strip()
                if name:
                    rec["case_name"] = name
                break

        # ── Lower court case number ──
        rec["lower_court_case_number"] = ""
        rec["originating_court"] = ""
        rec["lower_court_judge"] = ""

        # Try standard format first: "1:22-cr-20187-1"
        lc = C6_RE_LOWER_COURT_CASE.search(block)
        if lc:
            rec["lower_court_case_number"] = lc.group(1)
            after_lc = block[lc.end():]
            line_rest = after_lc.split("\n")[0].strip()
            parts = re.split(r"\s{2,}", line_rest)
            if parts:
                rec["originating_court"] = parts[0].strip()
            if len(parts) > 1:
                rec["lower_court_judge"] = parts[-1].strip()
        else:
            # Try immigration format: "a 212 951 791"
            imm = C6_RE_IMMIGRATION_CASE.search(block)
            if imm:
                rec["lower_court_case_number"] = imm.group(1)
                after_imm = block[imm.end():]
                line_rest = after_imm.split("\n")[0].strip()
                parts = re.split(r"\s{2,}", line_rest)
                if parts:
                    rec["originating_court"] = parts[0].strip()
                if len(parts) > 1:
                    rec["lower_court_judge"] = parts[-1].strip()

        # ── Time allotted ──
        rec["time_allotted"] = ""
        if C6_RE_ON_BRIEFS.search(block):
            rec["time_allotted"] = "Submitted On Briefs"
        else:
            ta_simple = C6_RE_TIME_ALLOTTED_SIMPLE.search(block)
            if ta_simple:
                rec["time_allotted"] = f"{ta_simple.group(1)} Minutes Per Side"
            else:
                ta_full = C6_RE_TIME_ALLOTTED_FULL.search(block)
                if ta_full:
                    text_inside = ta_full.group(1).strip()
                    if re.search(r"[Mm]inutes?", text_inside):
                        rec["time_allotted"] = text_inside

        # ── Case summary / description ──
        lines = block.split("\n")
        summary_lines = []
        capture = False
        for line in lines:
            stripped = line.strip()
            # Start capturing after the lower court info line
            if (C6_RE_LOWER_COURT_CASE.search(stripped)
                    or C6_RE_IMMIGRATION_CASE.search(stripped)):
                capture = True
                continue
            if capture:
                # Stop at time allotted parenthetical
                if (C6_RE_TIME_ALLOTTED_FULL.search(stripped)
                        or C6_RE_ON_BRIEFS.search(stripped)):
                    pre = re.split(r"\(", stripped)[0].strip()
                    if pre:
                        summary_lines.append(pre)
                    break
                if stripped:
                    summary_lines.append(stripped)
        rec["case_summary"] = " ".join(summary_lines).strip()

        return rec

    # ── Full PDF parsing ──

    def parse_pdf(self, pdf_bytes: bytes, pdf_url: str) -> List[Dict]:
        """
        Parse a complete PDF into a list of case record dicts.
        Splits by session first, then by case within each session.
        """
        full_text = self._extract_full_text(pdf_bytes)
        if not full_text:
            logger.warning(f"[C6] No text extracted from {pdf_url}")
            return []

        # Extract PDF-level metadata (posted date, etc.)
        pdf_meta = {
            "pdf_url": pdf_url,
            "pdf_filename": pdf_url.rsplit("/", 1)[-1],
            "posted_on": "",
        }
        posted = C6_RE_POSTED.search(full_text)
        if posted:
            pdf_meta["posted_on"] = f"{posted.group(1)} at {posted.group(2)}"
        else:
            revised = C6_RE_REVISED.search(full_text)
            if revised:
                pdf_meta["posted_on"] = (
                    f"Revised {revised.group(1)}, {revised.group(2).strip()}"
                )

        # Split into sessions
        sessions = self._split_into_sessions(full_text)
        logger.info(
            f"[C6] Found {len(sessions)} session(s) in "
            f"{pdf_url.rsplit('/', 1)[-1]}"
        )

        all_records = []
        for session_text in sessions:
            # Parse session header (date, courtroom, time, judges)
            session_header = self._parse_session_header(session_text)

            # Merge PDF-level and session-level metadata
            session_meta = {**pdf_meta, **session_header}

            # Split session into case blocks
            case_blocks = self._split_into_case_blocks(session_text)

            if not case_blocks:
                continue

            for block in case_blocks:
                rec = self._parse_case_block(block, session_meta)
                all_records.append(rec)

        if not all_records:
            logger.warning(
                f"[C6] No cases parsed from {pdf_url.rsplit('/', 1)[-1]}"
            )

        return all_records

    # ── Normalization to standard schema ──

    @staticmethod
    def normalize_cases(raw_cases: List[Dict]) -> List[Dict]:
        """
        Map raw parsed records to the standard 11-field output schema
        expected by app.py. Description contains only the case summary.
        """
        normalized = []
        for c in raw_cases:
            case_nums = c.get("case_numbers", [])
            judges = c.get("panel_judges", [])

            normalized.append({
                "Date": USCA6Scraper._format_date(c.get("argument_date", "")),
                "Case Number": ", ".join(case_nums) if case_nums else "",
                "Case Name": c.get("case_name", ""),
                "Nature of Case": "",
                "Court Name": C6_COURT_NAME,
                "Location": C6_DEFAULT_LOCATION,
                "Judges / Panel": ", ".join(judges) if judges else "",
                "Courtroom": c.get("courtroom", ""),
                "Purpose of Hearing": c.get("argument_type", "Oral Argument"),
                "Time": c.get("argument_time", ""),
                "Description": c.get("case_summary", ""),
            })
        return normalized

    # ── Public interface methods ──

    def scrape_all(self, progress_callback=None) -> List[Dict]:
        """
        Main entry point. Discovers PDFs, downloads, parses, normalizes.

        progress_callback signature:
            def fn(stage: str, label: str, current: int, total: int)
            stages: "pdfs"
        """
        pdf_links = self.discover_pdf_links()
        if not pdf_links:
            logger.warning("[C6] No PDF links found!")
            return []

        all_raw: List[Dict] = []
        total = len(pdf_links)

        for idx, link in enumerate(pdf_links):
            url = link["url"]
            label = link["link_text"][:60] if link["link_text"] else url

            if progress_callback:
                progress_callback("pdfs", label, idx + 1, total)

            pdf_bytes = self.download_pdf(url)
            if pdf_bytes is None:
                continue

            records = self.parse_pdf(pdf_bytes, url)
            if records:
                logger.info(
                    f"[C6] Parsed {len(records)} case(s) from "
                    f"{url.rsplit('/', 1)[-1]}"
                )
                all_raw.extend(records)

            time.sleep(self.delay)

        self._raw_data = all_raw
        normalized = self.normalize_cases(all_raw)
        logger.info(f"[C6] Total: {len(normalized)} cases from {total} PDFs")
        return normalized

    def scrape(self) -> pd.DataFrame:
        """Return scraped data as a DataFrame."""
        cases = self.scrape_all()
        return pd.DataFrame(cases)

    def get_raw_data(self):
        """Return the raw parsed data (before normalization)."""
        return self._raw_data