"""
Federal Circuit Court of Appeals — Oral Argument Calendar Scraper
Scrapes PDF schedules from cafc.uscourts.gov and normalizes to standard 11-field schema.

PDF Structure:
  PANEL X: DayOfWeek, Month DD, YYYY, HH:MM A.M./P.M., Courtroom NNN
  CaseNum  AgencyCode  CaseName [argued/on the briefs]

Standard 11-field output schema:
  Date | Case Number | Case Name | Nature of Case | Court Name |
  Location | Judges / Panel | Courtroom | Purpose of Hearing |
  Time | Description
"""

import os
import re
import ssl
import io
import urllib3
import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from typing import List, Dict, Optional, Callable
from datetime import datetime

from utils.helpers import logger
from utils.constants import DEFAULT_CAFC_SCHEDULED_CASES, DEFAULT_CAFC_BASE_URL

# Suppress SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────
CAFC_COURT_NAME = "United States Court of Appeals for the Federal Circuit"
CAFC_DEFAULT_LOCATION = "Washington, D.C."

# Agency code → Nature of Case mapping
AGENCY_CODE_MAP = {
    "PTO":  "Patent Trial and Appeal Board",
    "DCT":  "District Court",
    "CAVC": "Court of Appeals for Veterans Claims",
    "CIT":  "Court of International Trade",
    "CFC":  "Court of Federal Claims",
    "OCT":  "Originating Court/Tribunal",
    "BCA":  "Board of Contract Appeals",
    "MSPB": "Merit Systems Protection Board",
    "DVA":  "Department of Veterans Affairs",
    "ITC":  "International Trade Commission",
}

# All agency codes as a regex alternation (longest first to avoid partial matches)
_AGENCY_CODES_SORTED = sorted(AGENCY_CODE_MAP.keys(), key=len, reverse=True)
AGENCY_CODE_RE = re.compile(
    r'^(' + '|'.join(_AGENCY_CODES_SORTED) + r')\s+',
    re.IGNORECASE
)

# Purpose extraction: [argued], [on the briefs], [submitted], etc.
PURPOSE_BRACKET_RE = re.compile(
    r'\s*\[([^\]]+)\]\s*$',
    re.IGNORECASE
)

# Purpose mapping
PURPOSE_MAP = {
    "argued":         "Oral Argument",
    "on the briefs":  "On the Briefs",
    "submitted":      "Submitted",
}

# Panel header pattern:
# "PANEL A: Monday, June 1, 2026, 10:00 A.M., Courtroom 201"
# But pdfplumber may garble spaces: "B: Monday, June 1,2 026,1 0:00 A.M., Courtroom 402"
# So we need a flexible regex
PANEL_HEADER_RE = re.compile(
    r'(?:PANEL\s+)?([A-Z])\s*:\s*'                          # Panel letter
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*,?\s*'  # Day of week
    r'(\w+\s+\d{1,2}\s*,?\s*\d{4})\s*,?\s*'                # Date
    r'(\d{1,2}\s*:\s*\d{2}\s*(?:A\.?M\.?|P\.?M\.?))\s*,?\s*'  # Time
    r'(?:Courtroom|Room|Court\s*Room)\s*(\S+)',              # Courtroom
    re.IGNORECASE
)

# Fallback: simpler panel header (handles garbled text)
PANEL_HEADER_SIMPLE_RE = re.compile(
    r'(?:PANEL\s+)?([A-Z])\s*:\s*'
    r'((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*)\s*,?\s*'
    r'(\w+)\s+(\d{1,2})\s*,?\s*(\d{2,4})\s*,?\s*'
    r'(\d{1,2})\s*:\s*(\d{2})\s*(A\.?M\.?|P\.?M\.?)\s*,?\s*'
    r'(?:Courtroom|Room)\s*(\S+)',
    re.IGNORECASE
)

# Case number pattern
CASE_NUMBER_RE = re.compile(r'\b(\d{2}-\d{3,5})\b')

# Date pattern for standalone date lines
DATE_LINE_RE = re.compile(
    r'(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)'
    r'[,\s]+(\w+\s+\d{1,2},?\s*\d{4})',
    re.IGNORECASE
)

# Time pattern
TIME_RE = re.compile(r'(\d{1,2}\s*:\s*\d{2}\s*(?:A\.?M\.?|P\.?M\.?|AM|PM))', re.IGNORECASE)

# Courtroom pattern
COURTROOM_RE = re.compile(r'(?:Courtroom|Room|Court\s*Room)\s*(\S+)', re.IGNORECASE)


class SSLBypassAdapter(HTTPAdapter):
    """Adapter that fully disables SSL verification."""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)

    def send(self, request, **kwargs):
        kwargs["verify"] = False
        return super().send(request, **kwargs)


class USCAFCScraper:
    """Scraper for Federal Circuit oral argument schedules."""

    def __init__(self, scheduled_url: str = DEFAULT_CAFC_SCHEDULED_CASES,
                 verify_ssl: bool = False):
        self.scheduled_url = scheduled_url
        self.base_url = DEFAULT_CAFC_BASE_URL
        self.verify_ssl = verify_ssl
        self.session = self._get_session()
        self.raw_data = []

    def _get_session(self) -> requests.Session:
        session = requests.Session()
        session.verify = False
        session.mount("https://", SSLBypassAdapter())
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        })
        return session

    def discover_pdf_links(self) -> List[Dict]:
        """Discover PDF links from the scheduled cases page."""
        logger.info(f"Fetching Federal Circuit scheduled cases: {self.scheduled_url}")

        try:
            response = self.session.get(self.scheduled_url, timeout=30)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")

            pdf_links = []
            seen_urls = set()

            for a_tag in soup.find_all("a", href=True):
                href = a_tag["href"]
                if href.lower().endswith(".pdf"):
                    full_url = urljoin(self.base_url, href)
                    if full_url not in seen_urls:
                        seen_urls.add(full_url)
                        link_text = a_tag.get_text(strip=True)
                        pdf_links.append({
                            "url": full_url,
                            "label": link_text or os.path.basename(href),
                        })
                        logger.info(f"  Found PDF: {link_text} -> {full_url}")

            # Fallback: regex scan
            if not pdf_links:
                logger.warning("No PDF links found via <a> tags. Trying regex...")
                pdf_regex = re.compile(
                    r'(https?://[^\s"\'<>]+\.pdf|/[^\s"\'<>]+\.pdf)', re.IGNORECASE
                )
                matches = pdf_regex.findall(response.text)
                for match in matches:
                    full_url = urljoin(self.base_url, match)
                    if full_url not in seen_urls:
                        seen_urls.add(full_url)
                        pdf_links.append({
                            "url": full_url,
                            "label": os.path.basename(match),
                        })

            logger.info(f"  Discovered {len(pdf_links)} PDF(s)")
            return pdf_links

        except Exception as e:
            logger.error(f"Error discovering Federal Circuit PDFs: {e}")
            return []

    def _normalize_date(self, raw_date: str) -> str:
        """Normalize a raw date string to YYYY-MM-DD."""
        if not raw_date:
            return ""
        # Clean up garbled spaces: "June 1,2 026" -> "June 1, 2026"
        cleaned = re.sub(
            r'^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)[,\s]+',
            '', raw_date, flags=re.IGNORECASE
        ).strip()
        # Fix garbled year: "2 026" -> "2026"
        cleaned = re.sub(r'(\d),?\s*(\d)\s+(\d{3})', r'\1, \2\3', cleaned)
        cleaned = re.sub(r'(\d{1,2})\s*,?\s*(\d)\s+(\d{3})', r'\1, \2\3', cleaned)
        # Also try: "1,2 026" -> "1, 2026"
        cleaned = re.sub(r'(\d{1,2})\s*,\s*(\d)\s+(\d{3})\b', r'\1, \2\3', cleaned)

        for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
                     "%m/%d/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return raw_date

    def _normalize_time(self, raw_time: str) -> str:
        """Clean up garbled time: '1 0:00 A.M.' -> '10:00 A.M.'"""
        if not raw_time:
            return ""
        # Remove extra spaces within the time
        cleaned = re.sub(r'(\d)\s+(\d)', r'\1\2', raw_time.strip())
        # Normalize: "10:00A.M." -> "10:00 A.M."
        cleaned = re.sub(r'(\d{2})\s*(A\.?M\.?|P\.?M\.?)', r'\1 \2', cleaned, flags=re.IGNORECASE)
        return cleaned

    def _extract_case_name_parts(self, raw_name: str) -> Dict[str, str]:
        """
        Parse a raw case name like:
          'PTO Amsted Rail Company, Inc. v. Squires [argued]'
        Into:
          agency_code: 'PTO'
          case_name:   'Amsted Rail Company, Inc. v. Squires'
          purpose:     'Oral Argument'
        """
        result = {"agency_code": "", "case_name": raw_name.strip(), "purpose": ""}

        text = raw_name.strip()

        # 1. Extract [argued] / [on the briefs] from the end
        bracket_match = PURPOSE_BRACKET_RE.search(text)
        if bracket_match:
            bracket_text = bracket_match.group(1).strip().lower()
            # Remove closing paren if garbled: "[argued)" -> "argued)"
            bracket_text = bracket_text.rstrip(")")
            result["purpose"] = PURPOSE_MAP.get(bracket_text, bracket_text.title())
            text = text[:bracket_match.start()].strip()

        # Also handle garbled bracket: "[argued)" with paren instead of bracket
        paren_match = re.search(r'\s*\[([^\]]*)\)\s*$', text)
        if paren_match and not bracket_match:
            bracket_text = paren_match.group(1).strip().lower()
            result["purpose"] = PURPOSE_MAP.get(bracket_text, bracket_text.title())
            text = text[:paren_match.start()].strip()

        # 2. Extract agency code prefix
        agency_match = AGENCY_CODE_RE.match(text)
        if agency_match:
            code = agency_match.group(1).upper()
            result["agency_code"] = code
            text = text[agency_match.end():].strip()

        result["case_name"] = text.strip()
        return result

    def _parse_panel_header(self, line: str) -> Optional[Dict[str, str]]:
        """
        Parse a panel header line like:
          'PANEL A: Monday, June 1, 2026, 10:00 A.M., Courtroom 201'
        or garbled:
          'B: Monday, June 1,2 026,1 0:00 A.M., Courtroom 402'
          'C: Tuesday,J une 2,2 026, 10:00 A.M., Courtroom 201'

        Returns dict with: panel, date, time, courtroom
        """
        if not line:
            return None

        # Try the full regex first
        m = PANEL_HEADER_RE.search(line)
        if m:
            panel_letter = m.group(1).upper()
            raw_date = m.group(2).strip()
            raw_time = m.group(3).strip()
            courtroom = m.group(4).strip()
            return {
                "panel": f"Panel {panel_letter}",
                "date": self._normalize_date(raw_date),
                "time": self._normalize_time(raw_time),
                "courtroom": f"Courtroom {courtroom}",
            }

        # Try simpler approach: look for key markers
        # Check if line starts with a panel letter pattern
        panel_letter_match = re.match(r'(?:PANEL\s+)?([A-Z])\s*:', line, re.IGNORECASE)
        if not panel_letter_match:
            return None

        panel_letter = panel_letter_match.group(1).upper()
        rest = line[panel_letter_match.end():].strip()

        # Extract courtroom
        courtroom = ""
        cr_match = COURTROOM_RE.search(rest)
        if cr_match:
            courtroom = f"Courtroom {cr_match.group(1).strip()}"

        # Extract time — find pattern like "10:00 A.M." or garbled "1 0:00 A.M."
        time_str = ""
        # First try normal time
        time_match = TIME_RE.search(rest)
        if time_match:
            time_str = self._normalize_time(time_match.group(1))
        else:
            # Try garbled: "1 0:00 A.M."
            garbled_time = re.search(r'(\d)\s+(\d:\d{2})\s*(A\.?M\.?|P\.?M\.?)', rest, re.IGNORECASE)
            if garbled_time:
                time_str = f"{garbled_time.group(1)}{garbled_time.group(2)} {garbled_time.group(3)}"

        # Extract date — find month + day + year
        date_str = ""
        # Handle garbled: "June 1,2 026" or "J une 2,2 026"
        # First, try to reconstruct by removing stray spaces in month names
        rest_cleaned = rest
        # Fix "J une" -> "June", "J uly" -> "July", etc.
        rest_cleaned = re.sub(r'\bJ\s+une\b', 'June', rest_cleaned, flags=re.IGNORECASE)
        rest_cleaned = re.sub(r'\bJ\s+uly\b', 'July', rest_cleaned, flags=re.IGNORECASE)
        rest_cleaned = re.sub(r'\bA\s+ugust\b', 'August', rest_cleaned, flags=re.IGNORECASE)
        rest_cleaned = re.sub(r'\bS\s+eptember\b', 'September', rest_cleaned, flags=re.IGNORECASE)
        rest_cleaned = re.sub(r'\bO\s+ctober\b', 'October', rest_cleaned, flags=re.IGNORECASE)
        rest_cleaned = re.sub(r'\bN\s+ovember\b', 'November', rest_cleaned, flags=re.IGNORECASE)
        rest_cleaned = re.sub(r'\bD\s+ecember\b', 'December', rest_cleaned, flags=re.IGNORECASE)
        rest_cleaned = re.sub(r'\bF\s+ebruary\b', 'February', rest_cleaned, flags=re.IGNORECASE)
        rest_cleaned = re.sub(r'\bM\s+arch\b', 'March', rest_cleaned, flags=re.IGNORECASE)
        rest_cleaned = re.sub(r'\bA\s+pril\b', 'April', rest_cleaned, flags=re.IGNORECASE)
        rest_cleaned = re.sub(r'\bM\s+ay\b', 'May', rest_cleaned, flags=re.IGNORECASE)
        # Fix garbled year: "2 026" -> "2026", "1,2 026" -> "1, 2026"
        rest_cleaned = re.sub(r'(\d),\s*(\d)\s+(\d{3})\b', r'\1, \2\3', rest_cleaned)

        date_match = re.search(
            r'(January|February|March|April|May|June|July|August|September|October|November|December)'
            r'\s+(\d{1,2})\s*,?\s*(\d{4})',
            rest_cleaned, re.IGNORECASE
        )
        if date_match:
            date_str = self._normalize_date(
                f"{date_match.group(1)} {date_match.group(2)}, {date_match.group(3)}"
            )

        return {
            "panel": f"Panel {panel_letter}",
            "date": date_str,
            "time": time_str,
            "courtroom": courtroom,
        }

    def parse_case_pdf(self, pdf_url: str, label: str) -> List[Dict]:
        """Download and parse a single PDF into standardized case records."""
        logger.info(f"  Downloading: {label}")

        try:
            response = self.session.get(pdf_url, timeout=60)
            response.raise_for_status()

            try:
                import pdfplumber
            except ImportError:
                logger.warning("pdfplumber not installed. Install with: pip install pdfplumber")
                return []

            cases = []

            with pdfplumber.open(io.BytesIO(response.content)) as pdf:
                full_text = ""
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        full_text += text + "\n"

                if full_text:
                    self.raw_data.append({
                        'url': pdf_url, 'label': label, 'text': full_text[:5000]
                    })
                    cases = self._parse_pdf_text(full_text)

            logger.info(f"    Extracted {len(cases)} case(s)")
            return cases

        except Exception as e:
            logger.error(f"Error parsing Federal Circuit PDF: {e}")
            return []

    def _parse_pdf_text(self, text: str) -> List[Dict]:
        """
        Parse the full PDF text line-by-line.

        Tracks current panel context (panel letter, date, time, courtroom)
        and applies it to each case found underneath.
        """
        cases = []

        # Current context from panel headers
        current_panel = ""
        current_date = ""
        current_time = ""
        current_courtroom = ""

        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue

            # ── Check if this is a panel header line ──
            panel_info = self._parse_panel_header(line)
            if panel_info:
                current_panel = panel_info["panel"]
                if panel_info["date"]:
                    current_date = panel_info["date"]
                if panel_info["time"]:
                    current_time = panel_info["time"]
                if panel_info["courtroom"]:
                    current_courtroom = panel_info["courtroom"]
                continue

            # ── Check if this is a standalone date line ──
            date_match = DATE_LINE_RE.search(line)
            if date_match and not CASE_NUMBER_RE.search(line):
                current_date = self._normalize_date(date_match.group(0))
                continue

            # ── Check if this line has a case number ──
            cn_match = CASE_NUMBER_RE.search(line)
            if not cn_match:
                continue

            case_number = cn_match.group(1)

            # Everything after the case number is the raw case name
            after_cn = line[cn_match.end():].strip()
            # Remove leading punctuation
            after_cn = re.sub(r'^[\s,\-–—:]+', '', after_cn).strip()

            if not after_cn or len(after_cn) < 3:
                continue

            # ── Parse the case name parts ──
            parts = self._extract_case_name_parts(after_cn)

            # Map agency code to Nature of Case
            nature_of_case = ""
            if parts["agency_code"]:
                nature_of_case = AGENCY_CODE_MAP.get(
                    parts["agency_code"], parts["agency_code"]
                )

            # Determine purpose
            purpose = parts["purpose"] if parts["purpose"] else "Oral Argument"

            cases.append({
                "Date": current_date,
                "Case Number": case_number,
                "Case Name": parts["case_name"],
                "Nature of Case": nature_of_case,
                "Court Name": CAFC_COURT_NAME,
                "Location": CAFC_DEFAULT_LOCATION,
                "Judges / Panel": current_panel,
                "Courtroom": current_courtroom,
                "Purpose of Hearing": purpose,
                "Time": current_time,
                "Description": "",
            })

        return cases

    def scrape_all(self, progress_callback: Optional[Callable] = None) -> List[Dict]:
        """
        Scrape all Federal Circuit scheduled cases.
        Returns list of dicts in standard 11-field schema.
        """
        all_cases = []

        pdf_links = self.discover_pdf_links()
        if not pdf_links:
            logger.error("No PDFs found")
            return []

        total = len(pdf_links)

        for idx, pdf_info in enumerate(pdf_links, 1):
            if progress_callback:
                progress_callback(idx, total, pdf_info['label'])

            cases = self.parse_case_pdf(pdf_info['url'], pdf_info['label'])
            all_cases.extend(cases)

        # ── Deduplicate ──
        seen = set()
        unique = []
        for c in all_cases:
            key = f"{c.get('Case Number', '')}|{c.get('Date', '')}|{c.get('Judges / Panel', '')}"
            if key not in seen:
                seen.add(key)
                unique.append(c)

        logger.info(f"Federal Circuit: Total unique cases: {len(unique)}")
        return unique

    def get_raw_data(self) -> List[Dict]:
        return self.raw_data