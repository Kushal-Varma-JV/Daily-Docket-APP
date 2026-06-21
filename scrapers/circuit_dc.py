"""
DC Circuit Court of Appeals — Oral Argument Calendar Scraper
Standardized 11-field output schema.

The DC Circuit calendar uses a DataTables jQuery table. Each row has:
  td[0]: Sort key (hidden)
  td[1]: Sitting header — date, time, courtroom (div.print-bold) + judges (<i>)
  td[2]: Case order number ("1", "2", "3", "9")
  td[3]: Case number + case name + argument time (all in one cell, separated by divs)
"""

import re
import ssl
import urllib3
import requests
from datetime import datetime
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup
from typing import List, Dict, Optional, Callable
from urllib.parse import urljoin, urlparse, parse_qs

from utils.helpers import logger

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
C_DC_COURT_NAME = "United States Court of Appeals for the District of Columbia Circuit"
C_DC_DEFAULT_LOCATION = "Washington, DC"

C_DC_CALENDAR_PAGE = "https://www.cadc.uscourts.gov/oral-argument-calendar"
C_DC_FUTURE_DATES_URL = "https://media.cadc.uscourts.gov/calendar/calendar.php?cal=FutureDates"
C_DC_ENTIRE_TERM_URL = "https://media.cadc.uscourts.gov/calendar/calendar.php?cal=EntireTerm"

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------
RE_DATE = re.compile(
    r"((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
    r"[,\s]+\w+\s+\d{1,2},?\s*\d{4})",
    re.IGNORECASE,
)
RE_DATE_SHORT = re.compile(
    r"((?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)
RE_TIME = re.compile(r"(\d{1,2}:\d{2}\s*[AP]\.?M\.?)", re.IGNORECASE)
RE_CASE_NUM = re.compile(r"\b(\d{2,4}-\d{3,5})\b")
RE_COURTROOM = re.compile(r"(?:USCA\s+)?Courtroom\s+(\d+\w?)", re.IGNORECASE)
RE_ARG_TIME = re.compile(r"(\d+\s+minutes?\s+per\s+side)", re.IGNORECASE)
RE_RULE34J = re.compile(r"(To be decided w/o argument pursuant to Rule 34\(j\))", re.IGNORECASE)
RE_SEE_ORDER = re.compile(r"(See Order for Details)", re.IGNORECASE)


class SSLBypassAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)

    def send(self, request, **kwargs):
        kwargs["verify"] = False
        return super().send(request, **kwargs)


class USCADCScraper:
    """Scraper for DC Circuit oral argument calendar."""

    def __init__(self, calendar_url: str = C_DC_CALENDAR_PAGE, verify_ssl: bool = False):
        self.calendar_url = calendar_url
        self.verify_ssl = verify_ssl
        self.session = self._get_session()
        self.raw_html: List[str] = []

    def _get_session(self) -> requests.Session:
        session = requests.Session()
        session.verify = False
        session.mount("https://", SSLBypassAdapter())
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        return session

    def _get(self, url: str) -> Optional[requests.Response]:
        try:
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as exc:
            logger.error(f"[DC] Request failed for {url}: {exc}")
            return None

    # -----------------------------------------------------------------------
    # Discovery
    # -----------------------------------------------------------------------

    def discover_calendar_links(self) -> Dict[str, str]:
        logger.info(f"Discovering DC Circuit calendar links: {self.calendar_url}")
        resp = self._get(self.calendar_url)
        if not resp:
            return {}

        soup = BeautifulSoup(resp.text, "html.parser")
        calendar_links = {}

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            label = a_tag.get_text(strip=True)
            if "calendar" in href.lower() and (
                "cal=" in href
                or "calendar.php" in href.lower()
                or "sixtyday" in href.lower()
            ):
                full_url = urljoin(self.calendar_url, href)
                calendar_links[label] = full_url
                logger.info(f"  Found: '{label}' -> {full_url}")

        return calendar_links

    def get_future_dates_url(self, calendar_links: Dict[str, str]) -> tuple:
        for label, url in calendar_links.items():
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            cal_param = params.get("cal", [""])[0].lower()
            if "future" in cal_param:
                return url, label

        for label, url in calendar_links.items():
            if "future" in label.lower():
                return url, label

        for label, url in calendar_links.items():
            label_lower = label.lower()
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            cal_param = params.get("cal", [""])[0].lower()
            if "entire" not in cal_param and "today" not in cal_param:
                if "entire" not in label_lower and "today" not in label_lower:
                    return url, label

        return None, None

    # -----------------------------------------------------------------------
    # Fetching
    # -----------------------------------------------------------------------

    def fetch_calendar_html(self, url: str) -> str:
        logger.info(f"  Fetching: {url}")
        resp = self._get(url)
        if not resp:
            return ""

        html = resp.text

        soup = BeautifulSoup(html, "html.parser")
        iframe = soup.find("iframe", src=True)
        if iframe:
            iframe_url = urljoin(url, iframe["src"])
            logger.info(f"  Following iframe: {iframe_url}")
            resp2 = self._get(iframe_url)
            if resp2:
                html = resp2.text

        return html

    # -----------------------------------------------------------------------
    # Case info cell parser
    # -----------------------------------------------------------------------

    def _parse_case_info_cell(self, cell) -> Dict:
        """
        Parse a cell that contains case number + case name + argument time.

        The cell may have:
          - Bootstrap grid divs (col-xs-3 for case#, col-xs-5 for name, col-xs-4 for arg time)
          - Or just raw text: "24-5084Andrew Dudt v. Daniel Driscoll10 minutes per side"

        Returns dict with case_number, case_name, arg_time, description.
        """
        case_number = ""
        case_name = ""
        arg_time = ""
        description = ""

        # --- Method 1: Try Bootstrap grid divs ---
        grid_divs = cell.find_all("div", class_=re.compile(r"col-"))
        if len(grid_divs) >= 2:
            for div in grid_divs:
                classes = " ".join(div.get("class", []))
                div_text = div.get_text(strip=True)
                if not div_text:
                    continue

                # Narrow column = case number (col-*-2 or col-*-3)
                if re.search(r"col-\w+-[1-3]\b", classes):
                    cn_match = RE_CASE_NUM.search(div_text)
                    if cn_match:
                        case_number = cn_match.group(1)

                # Wide column = case name (col-*-4 through col-*-7)
                elif re.search(r"col-\w+-[4-7]\b", classes):
                    if not case_name:
                        case_name = div_text

                # Medium column = arg time (col-*-3 or col-*-4, but with time text)
                elif RE_ARG_TIME.search(div_text) or RE_RULE34J.search(div_text) or RE_SEE_ORDER.search(div_text):
                    at_match = RE_ARG_TIME.search(div_text)
                    r34_match = RE_RULE34J.search(div_text)
                    so_match = RE_SEE_ORDER.search(div_text)
                    if at_match:
                        arg_time = at_match.group(1)
                    elif r34_match:
                        description = r34_match.group(1)
                    elif so_match:
                        description = so_match.group(1)

            if case_number:
                return {
                    "case_number": case_number,
                    "case_name": case_name,
                    "arg_time": arg_time,
                    "description": description,
                }

        # --- Method 2: Use separator="\n" to split child elements ---
        lines = []
        for child in cell.children:
            if hasattr(child, 'get_text'):
                t = child.get_text(strip=True)
                if t:
                    lines.append(t)
            elif isinstance(child, str) and child.strip():
                lines.append(child.strip())

        if len(lines) >= 2:
            # Line 0 or 1 should have case number
            for i, line in enumerate(lines):
                cn_match = RE_CASE_NUM.search(line)
                if cn_match:
                    case_number = cn_match.group(1)
                    # Remaining text on same line after case number
                    remainder = line[cn_match.end():].strip()
                    if remainder:
                        case_name = remainder

                    # Next lines
                    for j in range(i + 1, len(lines)):
                        next_line = lines[j].strip()
                        if not next_line:
                            continue
                        if not case_name:
                            # Check if this line is arg time or case name
                            if RE_ARG_TIME.search(next_line):
                                arg_time = RE_ARG_TIME.search(next_line).group(1)
                            elif RE_RULE34J.search(next_line):
                                description = RE_RULE34J.search(next_line).group(1)
                            elif RE_SEE_ORDER.search(next_line):
                                description = RE_SEE_ORDER.search(next_line).group(1)
                            else:
                                case_name = next_line
                        else:
                            # Already have case name, this must be arg time / description
                            if RE_ARG_TIME.search(next_line):
                                arg_time = RE_ARG_TIME.search(next_line).group(1)
                            elif RE_RULE34J.search(next_line):
                                description = RE_RULE34J.search(next_line).group(1)
                            elif RE_SEE_ORDER.search(next_line):
                                description = RE_SEE_ORDER.search(next_line).group(1)
                    break

            if case_number:
                return {
                    "case_number": case_number,
                    "case_name": case_name,
                    "arg_time": arg_time,
                    "description": description,
                }

        # --- Method 3: Raw text with regex splitting ---
        raw_text = cell.get_text(strip=True)
        if not raw_text:
            return None

        cn_match = RE_CASE_NUM.search(raw_text)
        if not cn_match:
            return None

        case_number = cn_match.group(1)
        after_case_num = raw_text[cn_match.end():].strip()

        if not after_case_num:
            return {
                "case_number": case_number,
                "case_name": "",
                "arg_time": "",
                "description": "",
            }

        # Try to split off arg time / disposition from the end
        at_match = RE_ARG_TIME.search(after_case_num)
        r34_match = RE_RULE34J.search(after_case_num)
        so_match = RE_SEE_ORDER.search(after_case_num)

        if at_match:
            case_name = after_case_num[:at_match.start()].strip()
            arg_time = at_match.group(1)
        elif r34_match:
            case_name = after_case_num[:r34_match.start()].strip()
            description = r34_match.group(1)
        elif so_match:
            case_name = after_case_num[:so_match.start()].strip()
            description = so_match.group(1)
        else:
            case_name = after_case_num

        return {
            "case_number": case_number,
            "case_name": case_name,
            "arg_time": arg_time,
            "description": description,
        }

    # -----------------------------------------------------------------------
    # Main parser
    # -----------------------------------------------------------------------

    def parse_calendar(self, html: str, source_url: str) -> List[Dict]:
        """Parse the DC Circuit calendar HTML table."""
        soup = BeautifulSoup(html, "html.parser")
        cases = []

        # Find table
        table = soup.find("table", id="calendarentries")
        if not table:
            table = soup.find("table", class_="table")
        if not table:
            for t in soup.find_all("table"):
                rows = t.find_all("tr")
                if len(rows) > 2:
                    table = t
                    break
        if not table:
            logger.warning("[DC] No calendar table found")
            return []

        tbody = table.find("tbody") or table
        rows = tbody.find_all("tr")
        logger.info(f"  [DC] Found {len(rows)} table rows")

        # Log first few rows for debugging
        for i, row in enumerate(rows[:5]):
            cells = row.find_all("td")
            logger.info(f"  [DC] Row {i}: {len(cells)} cells")
            for j, cell in enumerate(cells):
                cell_text = cell.get_text(strip=True)[:100]
                logger.info(f"    td[{j}]: {cell_text}")

        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 4:
                continue

            # ---- td[1]: Sitting header ----
            header_cell = cells[1]
            date_str = ""
            time_str = ""
            courtroom = ""
            judges = ""

            bold_div = header_cell.find("div", class_="print-bold")
            if bold_div:
                header_text = bold_div.get_text(strip=True)

                header_match = re.match(
                    r'(.+?\d{4})\s+(\d{1,2}:\d{2}\s*[AP]\.?M\.?)\s*(.*)',
                    header_text,
                    re.IGNORECASE,
                )
                if header_match:
                    date_str = header_match.group(1).strip()
                    time_str = header_match.group(2).strip()
                    courtroom = header_match.group(3).strip()
                else:
                    dm = RE_DATE.search(header_text)
                    if dm:
                        date_str = dm.group(1).strip()
                    tm = RE_TIME.search(header_text)
                    if tm:
                        time_str = tm.group(1).strip()
                    cm = RE_COURTROOM.search(header_text)
                    if cm:
                        courtroom = f"Courtroom {cm.group(1)}"
            else:
                header_text = header_cell.get_text(strip=True)
                dm = RE_DATE.search(header_text)
                if dm:
                    date_str = dm.group(1).strip()
                tm = RE_TIME.search(header_text)
                if tm:
                    time_str = tm.group(1).strip()
                cm = RE_COURTROOM.search(header_text)
                if cm:
                    courtroom = f"Courtroom {cm.group(1)}"

            # Judges from <i>
            italic = header_cell.find("i")
            if italic:
                judges_text = italic.get_text(strip=True)
                cleaned = re.sub(r'^Judges?\s*', '', judges_text, flags=re.IGNORECASE).strip()
                if cleaned:
                    parts = re.split(r',\s*(?:and\s+)?|\s+and\s+', cleaned)
                    judges = ", ".join(p.strip() for p in parts if p.strip())

            # ---- Determine which cell has case info ----
            # Try td[3] first — if it has 5+ columns, case info is in td[3] with case# in td[3] and name in td[4]
            # If td[3] contains case# + name + arg time all together, parse it as combined

            case_info = None

            if len(cells) >= 5:
                # Check if td[3] is ONLY a case number and td[4] has the name
                td3_text = cells[3].get_text(strip=True)
                td4_text = cells[4].get_text(strip=True) if len(cells) > 4 else ""

                if RE_CASE_NUM.match(td3_text) and len(td3_text) < 15 and td4_text:
                    # td[3] = case number only, td[4] = case name + arg time
                    case_number = RE_CASE_NUM.match(td3_text).group(1)

                    # Parse td[4] for case name and arg time
                    case_name = ""
                    arg_time = ""
                    description = ""

                    # Use separator="\n" to split lines
                    td4_lines = [l.strip() for l in cells[4].get_text(separator="\n").splitlines() if l.strip()]

                    if td4_lines:
                        case_name = td4_lines[0]
                        for line in td4_lines[1:]:
                            at_match = RE_ARG_TIME.search(line)
                            r34_match = RE_RULE34J.search(line)
                            so_match = RE_SEE_ORDER.search(line)
                            if at_match:
                                arg_time = at_match.group(1)
                            elif r34_match:
                                description = r34_match.group(1)
                            elif so_match:
                                description = so_match.group(1)

                    case_info = {
                        "case_number": case_number,
                        "case_name": case_name,
                        "arg_time": arg_time,
                        "description": description,
                    }
                else:
                    # td[3] has everything combined — parse it
                    case_info = self._parse_case_info_cell(cells[3])

                    # If that didn't work, try td[4]
                    if not case_info and len(cells) > 4:
                        case_info = self._parse_case_info_cell(cells[4])
            else:
                # Only 4 columns — td[3] has everything
                case_info = self._parse_case_info_cell(cells[3])

            if not case_info or not case_info.get("case_number"):
                continue

            cases.append({
                "case_number": case_info["case_number"],
                "case_name": case_info.get("case_name", ""),
                "date": date_str,
                "time": time_str,
                "courtroom": courtroom,
                "judges": judges,
                "arg_time": case_info.get("arg_time", ""),
                "description": case_info.get("description", ""),
            })

        logger.info(f"  [DC] Extracted {len(cases)} cases")
        return cases

    # -----------------------------------------------------------------------
    # Normalization
    # -----------------------------------------------------------------------

    @staticmethod
    def _format_date(raw_date: str) -> str:
        if not raw_date:
            return ""
        raw_date = raw_date.strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", raw_date):
            return raw_date

        cleaned = re.sub(
            r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)[,\s]+",
            "", raw_date, flags=re.IGNORECASE,
        ).strip()

        for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue

        m = RE_DATE_SHORT.search(raw_date)
        if m:
            for fmt in ("%B %d, %Y", "%B %d %Y"):
                try:
                    return datetime.strptime(m.group(1), fmt).strftime("%Y-%m-%d")
                except ValueError:
                    continue
        return raw_date

    @staticmethod
    def _normalize_time(raw_time: str) -> str:
        if not raw_time:
            return ""
        raw_time = raw_time.strip()
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
    def _normalize_courtroom(raw_courtroom: str) -> str:
        if not raw_courtroom:
            return ""
        cr = raw_courtroom.strip()
        m = RE_COURTROOM.search(cr)
        if m:
            return f"Courtroom {m.group(1)}"
        return cr

    @staticmethod
    def _build_description(arg_time: str, description: str) -> str:
        parts = []
        if arg_time:
            parts.append(f"Argument time: {arg_time.strip()}")
        if description:
            parts.append(description.strip())
        return "; ".join(parts)

    def _to_standard_schema(self, raw_cases: List[Dict]) -> List[Dict]:
        normalized = []
        for r in raw_cases:
            normalized.append({
                "Case Name":          r.get("case_name", "").strip(),
                "Case Number":        r.get("case_number", "").strip(),
                "Nature of Case":     "",
                "Court Name":         C_DC_COURT_NAME,
                "Location":           C_DC_DEFAULT_LOCATION,
                "Judges / Panel":     r.get("judges", "").strip(),
                "Courtroom":          self._normalize_courtroom(r.get("courtroom", "")),
                "Purpose of Hearing": "Oral Argument",
                "Date":               self._format_date(r.get("date", "")),
                "Time":               self._normalize_time(r.get("time", "")),
                "Description":        self._build_description(
                                          r.get("arg_time", ""),
                                          r.get("description", ""),
                                      ),
            })
        return normalized

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def scrape(self, progress_callback: Optional[Callable] = None) -> List[Dict]:
        return self.scrape_all(progress_callback)

    def scrape_all(self, progress_callback: Optional[Callable] = None) -> List[Dict]:
        all_cases = []

        # Step 1: Discover
        if progress_callback:
            progress_callback(1, 5, "Discovering calendar links")

        calendar_links = self.discover_calendar_links()
        future_url, future_label = (None, None)
        if calendar_links:
            future_url, future_label = self.get_future_dates_url(calendar_links)

        # Step 2: Build URL list
        urls_to_try = []
        seen_urls = set()

        if future_url:
            urls_to_try.append((future_url, future_label))
            seen_urls.add(future_url)

        for url, label in [
            (C_DC_FUTURE_DATES_URL, "Future Dates (direct)"),
            (C_DC_ENTIRE_TERM_URL, "Entire Term (direct)"),
        ]:
            if url not in seen_urls:
                urls_to_try.append((url, label))
                seen_urls.add(url)

        logger.info(f"[DC] Will try {len(urls_to_try)} URLs")

        # Step 3: Try each URL
        for idx, (url, label) in enumerate(urls_to_try, 1):
            if progress_callback:
                progress_callback(idx + 1, len(urls_to_try) + 2, f"Trying: {label}")

            logger.info(f"[DC] Trying URL {idx}/{len(urls_to_try)}: {label} -> {url}")

            html = self.fetch_calendar_html(url)
            if not html:
                continue

            self.raw_html.append(html)
            cases = self.parse_calendar(html, url)

            if cases:
                logger.info(f"[DC] SUCCESS: {len(cases)} cases from {label}")
                all_cases = cases
                break
            else:
                logger.warning(f"[DC] No cases found from {label}")

        # Step 4: Normalize
        if progress_callback:
            progress_callback(len(urls_to_try) + 2, len(urls_to_try) + 2, "Normalizing results")

        normalized = self._to_standard_schema(all_cases)

        # Deduplicate
        seen = set()
        unique = []
        for case in normalized:
            key = (case["Case Number"], case["Date"], case["Time"])
            if key not in seen:
                seen.add(key)
                unique.append(case)

        logger.info(f"[DC] Final: {len(unique)} cases ({len(normalized)} before dedup)")

        if progress_callback:
            progress_callback(
                len(urls_to_try) + 2,
                len(urls_to_try) + 2,
                f"Complete — {len(unique)} cases",
            )

        return unique

    def get_raw_html(self) -> List[str]:
        return self.raw_html