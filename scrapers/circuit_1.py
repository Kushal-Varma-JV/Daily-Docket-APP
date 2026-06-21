"""
First Circuit Court of Appeals — Oral Argument Calendar Scraper.
Parses a single PDF from ca1.uscourts.gov.

Standardized fields:
  - case_name          : Names of parties involved
  - case_number        : Docket number (e.g. 24-1234)
  - nature_of_case     : Not available from this source → always ""
  - court_name         : Always "United States Court of Appeals for the First Circuit"
  - location           : City derived from PDF (Boston, MA or San Juan, Puerto Rico)
  - judges_panel       : Panel of judges from "BEFORE JUDGES:" line
  - courtroom          : Courtroom info if present in PDF, else ""
  - purpose_of_hearing : Always "Oral Argument" (this PDF only lists arguments)
  - date               : ISO 8601 date string (YYYY-MM-DD)
  - time               : Hearing time from PDF (e.g. "9:30 A.M.")
  - description        : Not available from this source → always ""
"""

import re
import io
from datetime import datetime
from typing import List, Dict

from scrapers.base import _make_ssl_session
from utils.helpers import HAS_PDFPLUMBER, HAS_PYPDF2, logger

if HAS_PYPDF2:
    from PyPDF2 import PdfReader
if HAS_PDFPLUMBER:
    import pdfplumber


# ---------------------------------------------------------------------------
# PDF download & text extraction (unchanged logic)
# ---------------------------------------------------------------------------

def fetch_pdf_bytes(url: str, timeout: int = 30) -> bytes:
    session = _make_ssl_session()
    resp = session.get(url, timeout=timeout, stream=True)
    resp.raise_for_status()
    chunks = []
    for chunk in resp.iter_content(chunk_size=1024 * 64):
        if chunk:
            chunks.append(chunk)
    return b"".join(chunks)


def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    if HAS_PYPDF2:
        with io.BytesIO(pdf_bytes) as bio:
            reader = PdfReader(bio)
            texts = []
            for page in reader.pages:
                try:
                    txt = page.extract_text() or ""
                except Exception:
                    txt = ""
                texts.append(txt)
            return "\n".join(texts)
    elif HAS_PDFPLUMBER:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            texts = []
            for page in pdf.pages:
                txt = page.extract_text() or ""
                texts.append(txt)
            return "\n".join(texts)
    return ""


# ---------------------------------------------------------------------------
# Date helper
# ---------------------------------------------------------------------------

def parse_date_to_iso(date_str: str) -> str:
    """
    Convert 'MONDAY, FEBRUARY 2, 2026' → '2026-02-02' (ISO 8601).
    Returns the original string if parsing fails.
    """
    try:
        # Strip any trailing time portion ("AT 9:30 A.M.")
        date_str = date_str.split(' AT ')[0].strip()
        date_str = date_str.replace(',', '').strip()

        # Try with day-of-week first, then without
        for fmt in ("%A %B %d %Y", "%B %d %Y"):
            try:
                return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue

        logger.warning(f"Could not parse date '{date_str}'")
        return date_str
    except Exception as e:
        logger.warning(f"Could not parse date '{date_str}': {e}")
        return date_str


# ---------------------------------------------------------------------------
# Core parser — returns standardized dicts
# ---------------------------------------------------------------------------

def parse_calendar_data(text: str) -> List[Dict]:
    """
    Walk through the extracted PDF text and return one dict per case
    using the project-wide standardized field names.
    """
    cases: List[Dict] = []

    # ---- split on the DOCKET header that begins each session block ----
    docket_sections = re.split(
        r'DOCKET\s+TO BE CALLED\s+(.+?),\s+AT\s+(\d{1,2}:\d{2}\s+[AP]\.M\.)\s*\n\s*NUMBER\s+(.+?)(?=\n)',
        text
    )

    current_judges: str = ""
    current_location: str = "Boston, MA"  # sensible default

    for i in range(1, len(docket_sections), 4):
        if i + 3 >= len(docket_sections):
            continue

        date_str = docket_sections[i].strip()        # e.g. "MONDAY, MARCH 2, 2026"
        time_str = docket_sections[i + 1].strip()    # e.g. "9:30 A.M."
        location_raw = docket_sections[i + 2].strip() # location / courtroom line
        section_text = docket_sections[i + 3]

        # --- date ---
        iso_date = parse_date_to_iso(date_str)

        # --- location (city) ---
        if "PUERTO RICO" in location_raw.upper() or "SAN JUAN" in location_raw.upper():
            location = "San Juan, Puerto Rico"
        else:
            location = "Boston, MA"

        # --- courtroom ---
        courtroom = ""
        courtroom_match = re.search(
            r'(COURTROOM\b.*)',
            location_raw,
            re.IGNORECASE,
        )
        if courtroom_match:
            courtroom = courtroom_match.group(1).strip().title()
        elif "COURTHOUSE" in location_raw.upper():
            # e.g. "JOSÉ V. TOLEDO COURTHOUSE, SAN JUAN, PUERTO RICO"
            courtroom = location_raw.strip().title()

        # --- judges / panel ---
        # Format 1: "BEFORE JUDGES: Aframe, Lynch, and Dunlap"
        # Format 2: "BEFORE: Chief Judge Barron, Justice Breyer, and Judge Gelpí"
        judges_match = re.search(
            r'BEFORE(?:\s+JUDGES)?:\s*(.+?)(?=\n|$)',
            section_text,
            re.IGNORECASE,
        )
        if judges_match:
            current_judges = judges_match.group(1).strip()

        # --- individual cases inside this session block ---
        case_pattern = r'(\d{2}-\d{4})\s+(.+?)(?=\d{2}-\d{4}|DOCKET|By the Court:|$)'
        for match in re.finditer(case_pattern, section_text, re.DOTALL):
            case_num = match.group(1)
            case_text = match.group(2).strip()
            case_lines = case_text.split('\n')
            case_name = case_lines[0].strip() if case_lines else ""

            cases.append({
                # --- STANDARDIZED FIELDS ---
                "date":               iso_date,
                "case_number":        case_num,
                "case_name":          case_name,
                "nature_of_case":     "",
                "court_name":         "United States Court of Appeals for the First Circuit",
                "location":           location,
                "judges_panel":       current_judges if current_judges else "",
                "courtroom":          courtroom,
                "purpose_of_hearing": "Oral Argument",
                "time":               time_str,
                "description":        "",
            })

    return cases


# ---------------------------------------------------------------------------
# Scraper class
# ---------------------------------------------------------------------------

class FirstCircuitScraper:
    """
    Scraper for the First Circuit Court of Appeals oral argument calendar.
    """

    PDF_URL = "https://www.ca1.uscourts.gov/sites/ca1/files/OralArgCalendar.pdf"

    def __init__(self, url: str | None = None):
        self.url = url or self.PDF_URL
        self.raw_pdf_bytes: bytes | None = None
        self.raw_text: str | None = None

    def scrape_all(self, progress_callback=None) -> List[Dict]:
        """
        Fetch and parse the First Circuit calendar PDF.
        Returns a list of standardized case dictionaries.
        """
        if progress_callback:
            progress_callback("Downloading First Circuit PDF...")

        try:
            self.raw_pdf_bytes = fetch_pdf_bytes(self.url)

            if progress_callback:
                progress_callback("Extracting text from PDF...")

            self.raw_text = extract_text_from_pdf_bytes(self.raw_pdf_bytes)

            if progress_callback:
                progress_callback("Parsing calendar data...")

            cases = parse_calendar_data(self.raw_text)

            if progress_callback:
                progress_callback(f"✓ Found {len(cases)} cases")

            return cases

        except Exception as e:
            logger.error(f"Error scraping First Circuit: {e}")
            if progress_callback:
                progress_callback(f"✗ Error: {str(e)}")
            return []

    def get_raw_data(self) -> str:
        """Return raw extracted text for debugging."""
        return self.raw_text or "No data fetched yet"