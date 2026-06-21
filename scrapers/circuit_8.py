"""
Eighth Circuit Court of Appeals — Oral Argument Calendar Scraper.
Discovers and parses PDF calendars from ca8.uscourts.gov.
"""

import re
import io
import time
import requests
from datetime import datetime
from typing import List, Dict, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from scrapers.base import _make_ssl_session
from utils.constants import DEFAULT_C8_URL
from utils.helpers import HAS_PDFPLUMBER, HAS_PYPDF2, logger

if HAS_PDFPLUMBER:
    import pdfplumber
if HAS_PYPDF2:
    from PyPDF2 import PdfReader

# ── Constants ──
C8_COURT_NAME = "United States Court of Appeals for the Eighth Circuit"
C8_DEFAULT_LOCATION = "St. Louis, MO"

# ── Compiled patterns ──
C8_RE_DIVISION = re.compile(r"^DIVISION\s+(I{1,3}V?|IV|V)\s*$", re.IGNORECASE)
C8_RE_COURTROOM = re.compile(
    r"^(?:Courtroom|En Banc Courtroom|Southeast Courtroom|Northeast Courtroom|"
    r"Northwest Courtroom|Southwest Courtroom)\b.*", re.IGNORECASE,
)
C8_RE_FLOOR = re.compile(r"^\d+\w*\s+Floor\s*$", re.IGNORECASE)
C8_RE_DATE_LINE = re.compile(
    r"(MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY)"
    r"[,\s]+(\w+\s+\d{1,2}[,\s]+\d{4})",
    re.IGNORECASE,
)
C8_RE_TIME_ONLY = re.compile(
    r"^BEGINNING\s+AT\s+([\d:]+\s*[AP]\.?M\.?)\s*$", re.IGNORECASE
)
C8_RE_TIME_IN = re.compile(
    r"BEGINNING\s+AT\s+([\d:]+\s*[AP]\.?M\.?)", re.IGNORECASE
)
C8_RE_JUDGES = re.compile(r"^BEFORE\s+(?:JUDGES?)\s+(.+)", re.IGNORECASE)
C8_RE_CASE_NUM = re.compile(r"(\d{2}-\d{3,5})")
C8_RE_NO_ARG = re.compile(r"\bNO\s+ARG\b", re.IGNORECASE)
C8_RE_DISMISSED = re.compile(r"\bDISMISSED\b", re.IGNORECASE)
C8_RE_SUBMITTED = re.compile(r"\bSUBMITTED\b", re.IGNORECASE)
C8_RE_REMOVED = re.compile(r"\bREMOVED\b", re.IGNORECASE)
C8_RE_UPDATED = re.compile(r"Updated[:\s]*([\d/\-]+\d{2,4})", re.IGNORECASE)
C8_RE_ITEM_NUM = re.compile(r"^\d+[.,]\s*")
C8_RE_CLERK_LINE = re.compile(r"^[A-Z]{2,4}$")
C8_RE_SPECIAL_SESSION = re.compile(r"\*\*\s*SPECIAL\s+SESSION\s*\*\*", re.IGNORECASE)

C8_KNOWN_DISTRICTS = {
    "NE", "MN", "SD", "ND", "EA", "WA", "NI", "WM", "SI",
    "IA", "AR", "MO", "NB", "ED", "WD", "CD", "EM", "WN",
    "SE", "SW", "NW",
}


class USCA8Scraper:
    def __init__(self, index_url: str = DEFAULT_C8_URL, verify_ssl: bool = False):
        self.index_url = index_url
        self.verify_ssl = verify_ssl
        self.session = _make_ssl_session()
        self._raw_data = None

    # ── Date formatting ──

    @staticmethod
    def _format_date(raw_date: str) -> str:
        """
        Convert various date formats to YYYY-MM-DD.
          'April 13, 2026'  → '2026-04-13'
          'APRIL 13 2026'   → '2026-04-13'
          'April 13,2026'   → '2026-04-13'
        Returns original string if parsing fails.
        """
        if not raw_date:
            return ""
        cleaned = raw_date.strip().rstrip(",")
        # Normalize whitespace around comma
        cleaned = re.sub(r",\s*", ", ", cleaned)
        for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
            try:
                return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return raw_date

    # ── HTTP helper ──

    def _get(self, url: str) -> requests.Response:
        for attempt in range(3):
            try:
                resp = self.session.get(url, timeout=30)
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                logger.warning(f"[C8] Attempt {attempt + 1} failed for {url}: {e}")
                if attempt == 2:
                    raise
        raise RuntimeError("Unreachable")

    # ── PDF discovery ──

    @staticmethod
    def _is_argument_calendar_pdf(url: str, link_text: str) -> bool:
        url_lower = url.lower()
        text_lower = link_text.lower()
        reject_keywords = [
            "eeo", "edr", "attorney", "admission", "paygov", "amend",
            "appointment", "civil_appointment", "mediator", "handbook",
            "guide", "form", "rule", "procedure", "faq",
            "policy", "plan", "order", "standing", "oadates", "oa_dates",
            "oral argument dates", "session dates",
        ]
        for kw in reject_keywords:
            if kw in url_lower or kw in text_lower:
                return False
        if re.search(
            r"(january|february|march|april|may|june|july|august|"
            r"september|october|november|december)\s+\d{1,2}",
            text_lower,
        ):
            return True
        if any(kw in text_lower for kw in ["session", "calendar", "argument"]):
            return True
        filename = url_lower.split("/")[-1].split("?")[0]
        if re.match(
            r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\d{2}\w+\.pdf$",
            filename,
        ):
            return True
        return True

    def discover_pdf_links(self) -> List[Dict]:
        logger.info(f"[C8] Fetching index page: {self.index_url}")
        resp = self._get(self.index_url)
        soup = BeautifulSoup(resp.text, "html.parser")
        pdf_links = []
        seen = set()
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"].strip()
            full_url = urljoin(self.index_url, href)
            if not full_url.lower().endswith(".pdf"):
                continue
            if full_url in seen:
                continue
            seen.add(full_url)
            link_text = a_tag.get_text(strip=True)
            if self._is_argument_calendar_pdf(full_url, link_text):
                pdf_links.append({"url": full_url, "link_text": link_text})
        for tag in soup.find_all(["iframe", "embed", "object"]):
            src = tag.get("src") or tag.get("data") or ""
            full_url = urljoin(self.index_url, src.strip())
            if full_url.lower().endswith(".pdf") and full_url not in seen:
                seen.add(full_url)
                if self._is_argument_calendar_pdf(full_url, ""):
                    pdf_links.append({"url": full_url, "link_text": ""})
        logger.info(f"[C8] Discovered {len(pdf_links)} argument calendar PDF(s)")
        return pdf_links

    # ── Download ──

    def download_pdf(self, url: str) -> Optional[bytes]:
        try:
            resp = self._get(url)
            logger.info(
                f"[C8] Downloaded {url.rsplit('/', 1)[-1]} "
                f"({len(resp.content) / 1024:.1f} KB)"
            )
            return resp.content
        except Exception as e:
            logger.error(f"[C8] Failed to download {url}: {e}")
            return None

    # ── Text extraction ──

    @staticmethod
    def _extract_text(pdf_bytes: bytes) -> str:
        full_text = ""
        if HAS_PDFPLUMBER:
            try:
                with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                    for page in pdf.pages:
                        text = page.extract_text()
                        if text:
                            full_text += text + "\n"
            except Exception as e:
                logger.warning(f"[C8] pdfplumber failed: {e}")
        elif HAS_PYPDF2:
            try:
                reader = PdfReader(io.BytesIO(pdf_bytes))
                for page in reader.pages:
                    try:
                        txt = page.extract_text() or ""
                    except Exception:
                        txt = ""
                    full_text += txt + "\n"
            except Exception as e:
                logger.warning(f"[C8] PyPDF2 failed: {e}")
        return full_text.strip()

    # ── Page classification ──

    @staticmethod
    def _is_boilerplate_page(text: str) -> bool:
        """Detect cover page and notice-to-counsel pages."""
        signals = [
            "NOTICE TO ALL COUNSEL", "NOTICE TO COUNSEL",
            "argument calendar", "UNITED STATES COURT OF APPEALS",
            "The following cases are scheduled",
            "Please review the entire calendar",
        ]
        hits = sum(1 for s in signals if s.lower() in text.lower())
        case_numbers = C8_RE_CASE_NUM.findall(text)
        return hits >= 2 and len(case_numbers) < 2

    @staticmethod
    def _extract_cover_info(text: str) -> Dict:
        """Extract location, date range, term from cover/notice pages."""
        info: Dict = {}
        # Location: "ST. LOUIS, MISSOURI" or similar all-caps city line
        loc = re.search(r"\n\s*([A-Z][A-Z .]+,\s*[A-Z][A-Z ]+)\s*\n", text)
        if loc:
            raw_loc = loc.group(1).strip()
            # Normalize: "ST. LOUIS, MISSOURI" → "St. Louis, MO"
            info["location"] = raw_loc
        # Date range: "APRIL 13 – 17, 2026"
        dr = re.search(
            r"(\w+\s+\d{1,2}\s*[-\u2013\u2014]\s*(?:\w+\s+)?\d{1,2}[,\s]+\d{4})",
            text,
        )
        if dr:
            info["date_range"] = dr.group(1).strip()
        # Term
        term = re.search(
            r"(\d{4}\s*[-\u2013\u2014]\s*\d{4})\s*Term", text, re.IGNORECASE
        )
        if term:
            info["term"] = term.group(1).strip()
        # Updated date
        upd = C8_RE_UPDATED.search(text)
        if upd:
            info["last_updated"] = upd.group(1).strip()
        return info

    # ── Line classification ──

    @staticmethod
    def _classify_line(line: str) -> str:
        s = line.strip()
        if not s:
            return "BLANK"
        if C8_RE_DIVISION.match(s):
            return "DIVISION"
        if C8_RE_COURTROOM.match(s):
            return "COURTROOM"
        if C8_RE_FLOOR.match(s):
            return "FLOOR"
        if C8_RE_DATE_LINE.search(s):
            return "DATE"
        if C8_RE_TIME_ONLY.match(s):
            return "TIME"
        if C8_RE_JUDGES.match(s):
            return "JUDGES"
        if C8_RE_SPECIAL_SESSION.search(s):
            return "SPECIAL"
        if C8_RE_CLERK_LINE.match(s) and not C8_RE_CASE_NUM.search(s):
            return "CLERK"
        if C8_RE_CASE_NUM.search(s):
            return "CASE"
        return "TEXT"

    # ── Case block parsing ──

    @staticmethod
    def _parse_case_block(lines: List[str]) -> List[List[Dict]]:
        """
        Parse a block of case lines into structured entries.
        Each entry may contain multiple consolidated case numbers.
        Returns a list of entry groups, where each group is a list of case dicts.
        """
        # First, merge continuation lines into entries
        entries: List[str] = []
        current: Optional[str] = None
        for line in lines:
            s = line.strip()
            if not s:
                continue
            s_clean = C8_RE_ITEM_NUM.sub("", s).strip()
            if C8_RE_CASE_NUM.search(s_clean):
                if current is not None:
                    entries.append(current)
                current = s_clean
            else:
                # Continuation line (multi-line case name)
                if current is not None:
                    current += " " + s_clean
        if current is not None:
            entries.append(current)

        # Parse each entry
        results: List[List[Dict]] = []
        for entry in entries:
            case_nums = C8_RE_CASE_NUM.findall(entry)
            if not case_nums:
                continue

            # Determine status
            if C8_RE_NO_ARG.search(entry):
                status, arg_time = "NO ARG", None
            elif C8_RE_DISMISSED.search(entry):
                status, arg_time = "DISMISSED", None
            elif C8_RE_REMOVED.search(entry):
                status, arg_time = "REMOVED", None
            elif C8_RE_SUBMITTED.search(entry):
                status, arg_time = "SUBMITTED", None
            else:
                # Look for trailing number (argument minutes)
                m = re.search(r"\b(\d{1,3})\s*$", entry)
                if m and 1 <= int(m.group(1)) <= 120:
                    arg_time = int(m.group(1))
                    status = "ARG"
                else:
                    arg_time = None
                    status = "UNKNOWN"

            # Split entry into individual cases
            case_segments = re.split(r'(\d{2}-\d{3,5})', entry)
            parsed_cases: List[Dict] = []
            i = 1
            while i < len(case_segments) - 1:
                cn = case_segments[i]
                text_after = (
                    case_segments[i + 1].strip()
                    if i + 1 < len(case_segments)
                    else ""
                )
                # Check for district code
                district = None
                words = text_after.split()
                if words:
                    first = words[0].strip()
                    if first.upper() in C8_KNOWN_DISTRICTS or (
                        len(first) == 2 and first.isalpha() and first.isupper()
                    ):
                        district = first.upper()
                        text_after = " ".join(words[1:])
                # Clean case name
                name = text_after
                for pat in [
                    C8_RE_NO_ARG, C8_RE_DISMISSED,
                    C8_RE_REMOVED, C8_RE_SUBMITTED,
                ]:
                    name = pat.sub("", name)
                name = re.sub(r"\b\d{1,3}\s*$", "", name)
                name = re.sub(r"\s{2,}", " ", name).strip().rstrip(".,;: ")
                parsed_cases.append({
                    "case_number": cn,
                    "district": district,
                    "name": name,
                    "arg_time": arg_time,
                    "status": status,
                })
                i += 2
            results.append(parsed_cases)
        return results

    # ── Full PDF parsing ──

    def parse_pdf(self, pdf_bytes: bytes, pdf_url: str) -> Dict:
        """
        Parse a complete C8 argument calendar PDF.
        Returns a session dict with hearing_blocks containing cases.
        """
        session: Dict = {
            "source_url": pdf_url,
            "pdf_filename": pdf_url.rsplit("/", 1)[-1],
            "location": None,
            "date_range": None,
            "term": None,
            "last_updated": None,
            "hearing_blocks": [],
        }

        if not HAS_PDFPLUMBER:
            text = self._extract_text(pdf_bytes)
            session["raw_text"] = text
            return session

        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            page_texts = [p.extract_text() or "" for p in pdf.pages]

            # Extract cover info from first two pages
            cover = self._extract_cover_info("\n".join(page_texts[:2]))
            session["location"] = cover.get("location")
            session["date_range"] = cover.get("date_range")
            session["term"] = cover.get("term")
            session["last_updated"] = cover.get("last_updated")

            # Check all pages for updated date if not found
            if not session["last_updated"]:
                upd = C8_RE_UPDATED.search("\n".join(page_texts))
                if upd:
                    session["last_updated"] = upd.group(1).strip()

            # Collect all lines from non-boilerplate pages
            all_lines: List[str] = []
            for pg_idx, pg_text in enumerate(page_texts):
                if pg_idx == 0:
                    continue  # Skip cover page
                if self._is_boilerplate_page(pg_text) and pg_idx < 3:
                    continue  # Skip notice page
                for line in pg_text.split("\n"):
                    all_lines.append(line)

            # State machine: walk through lines
            current_division = None
            current_courtroom = None
            current_date = None
            current_time = None
            current_judges: List[str] = []
            current_clerk = None
            case_lines: List[str] = []
            group_counter = 0

            def flush_block() -> Optional[Dict]:
                nonlocal case_lines, group_counter
                if not case_lines:
                    return None
                parsed_entries = self._parse_case_block(case_lines)
                if not parsed_entries:
                    case_lines = []
                    return None
                block: Dict = {
                    "division": current_division,
                    "courtroom": current_courtroom,
                    "hearing_date": current_date,
                    "start_time": current_time,
                    "judges": list(current_judges),
                    "clerk_initials": current_clerk,
                    "cases": [],
                }
                for entry_cases in parsed_entries:
                    group_counter += 1
                    is_consolidated = len(entry_cases) > 1
                    for pc in entry_cases:
                        block["cases"].append({
                            "case_number": pc["case_number"],
                            "district_code": pc["district"],
                            "case_name": pc["name"],
                            "argument_minutes": pc["arg_time"],
                            "status": pc["status"],
                            "clerk_initials": current_clerk,
                            "group_id": (
                                group_counter if is_consolidated else None
                            ),
                        })
                case_lines = []
                return block

            for line in all_lines:
                s = line.strip()
                ltype = self._classify_line(s)

                if ltype == "BLANK":
                    continue
                elif ltype == "DIVISION":
                    blk = flush_block()
                    if blk:
                        session["hearing_blocks"].append(blk)
                    m = C8_RE_DIVISION.match(s)
                    current_division = m.group(1).upper()
                    current_courtroom = None
                    current_judges = []
                    current_clerk = None
                elif ltype == "COURTROOM":
                    current_courtroom = s
                elif ltype == "FLOOR":
                    if current_courtroom:
                        current_courtroom += ", " + s
                elif ltype == "DATE":
                    blk = flush_block()
                    if blk:
                        session["hearing_blocks"].append(blk)
                    m = C8_RE_DATE_LINE.search(s)
                    current_date = m.group(2).strip().rstrip(",")
                    tm = C8_RE_TIME_IN.search(s)
                    current_time = tm.group(1).strip() if tm else None
                    current_judges = []
                    current_clerk = None
                elif ltype == "TIME":
                    blk = flush_block()
                    if blk:
                        session["hearing_blocks"].append(blk)
                    m = C8_RE_TIME_ONLY.match(s)
                    current_time = m.group(1).strip()
                    current_judges = []
                    current_clerk = None
                elif ltype == "JUDGES":
                    m = C8_RE_JUDGES.match(s)
                    raw = m.group(1)
                    current_judges = [
                        j.strip().rstrip(",")
                        for j in re.split(r",\s*|\s+and\s+", raw)
                        if j.strip()
                    ]
                elif ltype == "CLERK":
                    current_clerk = s
                elif ltype == "SPECIAL":
                    # Skip special session markers
                    continue
                elif ltype == "CASE":
                    case_lines.append(s)
                elif ltype == "TEXT":
                    if case_lines:
                        case_lines.append(s)

            # Flush final block
            blk = flush_block()
            if blk:
                session["hearing_blocks"].append(blk)

        total = sum(len(b["cases"]) for b in session["hearing_blocks"])
        logger.info(
            f"[C8] Parsed {session['pdf_filename']}: "
            f"{len(session['hearing_blocks'])} blocks, {total} case entries"
        )
        return session

    # ── Normalization to standard schema ──

    @staticmethod
    def normalize_session(session: Dict) -> List[Dict]:
        """
        Map raw parsed session data to the standard 11-field output schema.
        """
        normalized: List[Dict] = []

        # Derive location from cover page
        raw_location = session.get("location") or ""
        # Normalize common patterns: "ST. LOUIS, MISSOURI" → "St. Louis, MO"
        location = raw_location
        if not location:
            location = C8_DEFAULT_LOCATION
        elif "LOUIS" in location.upper() and "MISSOURI" in location.upper():
            location = "St. Louis, MO"
        elif "PAUL" in location.upper() and "MINNESOTA" in location.upper():
            location = "St. Paul, MN"
        elif "OMAHA" in location.upper() and "NEBRASKA" in location.upper():
            location = "Omaha, NE"

        for block in session.get("hearing_blocks", []):
            judges_str = ", ".join(block.get("judges", []))

            # Build courtroom string with division
            courtroom = block.get("courtroom", "") or ""
            division = block.get("division", "")
            if division:
                courtroom = (
                    f"Division {division} — {courtroom}"
                    if courtroom
                    else f"Division {division}"
                )

            for c in block.get("cases", []):
                # Build description: status + duration + district
                desc_parts = []
                status = c.get("status", "")
                if status and status not in ("ARG", "UNKNOWN"):
                    desc_parts.append(status)
                arg_mins = c.get("argument_minutes")
                if arg_mins:
                    desc_parts.append(f"{arg_mins} Minutes Per Side")
                district = c.get("district_code")
                if district:
                    desc_parts.append(f"District: {district}")

                normalized.append({
                    "Date": USCA8Scraper._format_date(
                        block.get("hearing_date", "")
                    ),
                    "Case Number": c.get("case_number", ""),
                    "Case Name": c.get("case_name", ""),
                    "Nature of Case": "",
                    "Court Name": C8_COURT_NAME,
                    "Location": location,
                    "Judges / Panel": judges_str,
                    "Courtroom": courtroom,
                    "Purpose of Hearing": "Oral Argument",
                    "Time": block.get("start_time", "") or "",
                    "Description": " | ".join(desc_parts) if desc_parts else "",
                })
        return normalized

    # ── Public interface ──

    def scrape_all(self, progress_callback=None) -> List[Dict]:
        pdf_links = self.discover_pdf_links()
        if not pdf_links:
            logger.warning("[C8] No PDF links found on the index page!")
            return []

        all_normalized: List[Dict] = []
        total = len(pdf_links)

        for idx, link in enumerate(pdf_links):
            url = link["url"]
            label = link.get("link_text", url.rsplit("/", 1)[-1])[:60]

            if progress_callback:
                progress_callback(idx + 1, total, label)

            pdf_bytes = self.download_pdf(url)
            if pdf_bytes is None:
                continue

            try:
                session = self.parse_pdf(pdf_bytes, url)
                case_count = sum(
                    len(b["cases"])
                    for b in session.get("hearing_blocks", [])
                )
                if case_count == 0:
                    logger.info(
                        f"[C8] Skipping {session['pdf_filename']} — "
                        f"0 case entries"
                    )
                    continue
                normalized = self.normalize_session(session)
                all_normalized.extend(normalized)
            except Exception as e:
                logger.error(
                    f"[C8] Failed to parse {url}: {e}", exc_info=True
                )

            time.sleep(0.3)

        self._raw_data = all_normalized
        logger.info(
            f"[C8] Total: {len(all_normalized)} cases from {total} PDFs"
        )
        return all_normalized

    def get_raw_data(self):
        """Return the raw normalized data."""
        return self._raw_data

    def get_raw_texts(self) -> Dict[str, str]:
        pdf_links = self.discover_pdf_links()
        texts: Dict[str, str] = {}
        for link in pdf_links:
            pdf_bytes = self.download_pdf(link["url"])
            filename = link["url"].rsplit("/", 1)[-1]
            if pdf_bytes:
                texts[filename] = self._extract_text(pdf_bytes)
            else:
                texts[filename] = "(download failed)"
        return texts