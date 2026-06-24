"""
Scraper for the United States Court of Appeals for the Ninth Circuit.

The Ninth Circuit's website embeds oral argument calendar data as a
JavaScript variable `global_panel_sittings` in the HTML source of
/cases/calendar/. Individual sitting details (including case synopses)
are fetched from an AWS Lambda API.

Strategy (two-phase):
    Phase 1 — Fast metadata (single request to /cases/calendar/):
        1. GET /cases/calendar/
        2. Extract `global_panel_sittings` JSON from <script> tag via regex
        3. Flatten the nested structure into records with date + hl_code

    Phase 2 — Case synopses (one API call per date+location):
        4. For each unique (date, hl_code), call the Lambda API:
           https://avessdwb2hgbkch6hmpylkkcy40feibd.lambda-url.us-west-2.on.aws/
           ?date=YYYY-MM-DD&loc=XX
        5. The API returns JSON with panel_sittings containing full case
           data including the `synopsis` field
        6. Merge synopses + formatted_panel (judges) back into records

No Playwright, no BeautifulSoup needed. Pure requests + JSON.
"""

import json
import logging
import re
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

C9_COURT_NAME = "United States Court of Appeals for the Ninth Circuit"
C9_BASE_URL = "https://www.ca9.uscourts.gov"
C9_CALENDAR_PATH = "/cases/calendar/"

# Lambda API that serves sitting detail data (including synopses)
C9_LAMBDA_API = (
    "https://avessdwb2hgbkch6hmpylkkcy40feibd"
    ".lambda-url.us-west-2.on.aws/"
)

# Regex to extract the global_panel_sittings JSON from the page source.
C9_RE_PANEL_SITTINGS = re.compile(
    r"let\s+global_panel_sittings\s*=\s*(\[.*?\])\s*;?\s*"
    r"(?:console\.log|let\s|var\s|function\s|</script>)",
    re.DOTALL,
)

# Fallback: more permissive pattern
C9_RE_PANEL_SITTINGS_FALLBACK = re.compile(
    r"global_panel_sittings\s*=\s*(\[.*?\])\s*;",
    re.DOTALL,
)

# Location code → normalized "City, ST" mapping
C9_LOCATION_MAP = {
    "SF":   "San Francisco, CA",
    "PAS":  "Pasadena, CA",
    "SE":   "Seattle, WA",
    "PO":   "Portland, OR",
    "PHX":  "Phoenix, AZ",
    "HO":   "Honolulu, HI",
    "AKOF": "Anchorage, AK",
    "RNO":  "Reno, NV",
    "TUS":  "Tucson, AZ",
    "LV":   "Las Vegas, NV",
    "SAI":  "Saipan, MP",
    "GU":   "Hagatna, GU",
    "SJ":   "San Jose, CA",
    "MIS":  "Missoula, MT",
}

C9_DISPLAY_NAME_MAP = {
    "san francisco": "San Francisco, CA",
    "pasadena":      "Pasadena, CA",
    "seattle":       "Seattle, WA",
    "portland":      "Portland, OR",
    "phoenix":       "Phoenix, AZ",
    "honolulu":      "Honolulu, HI",
    "anchorage":     "Anchorage, AK",
    "reno":          "Reno, NV",
    "tucson":        "Tucson, AZ",
    "las vegas":     "Las Vegas, NV",
    "saipan":        "Saipan, MP",
    "hagatna":       "Hagatna, GU",
    "san jose":      "San Jose, CA",
    "missoula":      "Missoula, MT",
}

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SSL Bypass Adapter
# ---------------------------------------------------------------------------

class SSLBypassAdapter(requests.adapters.HTTPAdapter):
    """HTTPS adapter that disables SSL verification."""
    def send(self, *args, **kwargs):
        kwargs["verify"] = False
        return super().send(*args, **kwargs)


# ---------------------------------------------------------------------------
# Main Scraper Class
# ---------------------------------------------------------------------------

class USCA9Scraper:
    """
    Scraper for the Ninth Circuit Court of Appeals oral argument calendar.

    Usage:
        scraper = USCA9Scraper()
        cases = scraper.scrape()  # Returns List[Dict] with 11-field schema
    """

    def __init__(
        self,
        base_url: str = C9_BASE_URL,
        verify_ssl: bool = False,
        request_delay: float = 1.0,
        max_retries: int = 3,
        timeout: int = 30,
        fetch_descriptions: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.verify_ssl = verify_ssl
        self.request_delay = request_delay
        self.max_retries = max_retries
        self.timeout = timeout
        self.fetch_descriptions = fetch_descriptions

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        })
        if not verify_ssl:
            self.session.mount("https://", SSLBypassAdapter())
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # -----------------------------------------------------------------------
    # HTTP helpers
    # -----------------------------------------------------------------------

    def _get(self, url: str) -> Optional[requests.Response]:
        """GET with retries and delay."""
        for attempt in range(1, self.max_retries + 1):
            try:
                if attempt > 1:
                    time.sleep(self.request_delay * attempt)
                else:
                    time.sleep(self.request_delay)

                resp = self.session.get(
                    url,
                    timeout=self.timeout,
                    verify=self.verify_ssl,
                )
                resp.raise_for_status()
                return resp

            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response else "?"
                logger.warning(
                    "[C9] HTTP %s on attempt %d/%d for %s",
                    status, attempt, self.max_retries, url,
                )
                if status == 404:
                    return None
            except requests.exceptions.RequestException as exc:
                logger.warning(
                    "[C9] Request error on attempt %d/%d for %s: %s",
                    attempt, self.max_retries, url, exc,
                )

        logger.error(
            "[C9] All %d attempts failed for %s", self.max_retries, url
        )
        return None

    def _get_json(self, url: str) -> Optional[Any]:
        """GET and parse JSON response."""
        resp = self._get(url)
        if not resp:
            return None
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("[C9] Failed to parse JSON from %s: %s", url, exc)
            return None

    # -----------------------------------------------------------------------
    # JS → JSON converter (shared utility)
    # -----------------------------------------------------------------------

    @staticmethod
    def _js_object_to_json(js_text: str) -> str:
        """Convert JavaScript object notation to valid JSON."""
        result = []
        i = 0
        in_string = False
        string_char = None

        while i < len(js_text):
            ch = js_text[i]

            if in_string:
                result.append(ch)
                if ch == "\\" and i + 1 < len(js_text):
                    i += 1
                    result.append(js_text[i])
                elif ch == string_char:
                    in_string = False
                    string_char = None
                i += 1
                continue

            if ch in ('"', "'"):
                in_string = True
                string_char = ch
                result.append('"' if ch == "'" else ch)
                i += 1
                continue

            if ch.isalpha() or ch == "_":
                j = i
                while j < len(js_text) and (
                    js_text[j].isalnum() or js_text[j] == "_"
                ):
                    j += 1
                identifier = js_text[i:j]

                k = j
                while k < len(js_text) and js_text[k] in (
                    " ", "\t", "\n", "\r"
                ):
                    k += 1

                if k < len(js_text) and js_text[k] == ":":
                    result.append('"')
                    result.append(identifier)
                    result.append('"')
                    i = j
                    continue
                else:
                    result.append(ch)
                    i += 1
                    continue

            result.append(ch)
            i += 1

        text = "".join(result)
        text = re.sub(r",\s*([}\]])", r"\1", text)
        return text

    # -----------------------------------------------------------------------
    # Phase 1: Extract global_panel_sittings from main calendar page
    # -----------------------------------------------------------------------

    def _fetch_calendar_html(self) -> Optional[str]:
        """Fetch the calendar page HTML."""
        url = f"{self.base_url}{C9_CALENDAR_PATH}"
        resp = self._get(url)
        if not resp:
            return None
        return resp.text

    def _extract_panel_sittings(self, html: str) -> Optional[List[Dict]]:
        """Extract the global_panel_sittings JSON array from page HTML."""
        m = C9_RE_PANEL_SITTINGS.search(html)
        if not m:
            m = C9_RE_PANEL_SITTINGS_FALLBACK.search(html)

        if not m:
            logger.error(
                "[C9] Could not find global_panel_sittings in page source"
            )
            return None

        raw_json = m.group(1)
        valid_json = self._js_object_to_json(raw_json)

        try:
            data = json.loads(valid_json)
            logger.info(
                "[C9] Extracted %d date entries from global_panel_sittings",
                len(data),
            )
            return data
        except json.JSONDecodeError as exc:
            logger.error(
                "[C9] Failed to parse global_panel_sittings JSON: %s", exc
            )
            try:
                cleaned = re.sub(r",\s*([}\]])", r"\1", valid_json)
                data = json.loads(cleaned)
                logger.info(
                    "[C9] Parsed after trailing comma cleanup: %d entries",
                    len(data),
                )
                return data
            except json.JSONDecodeError as exc2:
                logger.error(
                    "[C9] Second parse attempt also failed: %s", exc2
                )
                return None

    def _flatten_sittings(self, sittings: List[Dict]) -> List[Dict]:
        """
        Flatten the nested global_panel_sittings structure into flat
        records. Each record has date, hl_code, location, courtroom,
        session_time, and basic case fields.
        """
        records = []

        for date_entry in sittings:
            hearing_date = date_entry.get("date", "")

            for location in date_entry.get("locations", []):
                hl_code = location.get("hl_code", "")
                display_name = location.get(
                    "location_display_name", ""
                )
                normalized_location = self._normalize_location(
                    hl_code, display_name
                )

                for courtroom_entry in location.get("courtrooms", []):
                    courtroom = courtroom_entry.get("courtroom", "")

                    for time_entry in courtroom_entry.get("times", []):
                        session_time = time_entry.get("time", "")

                        for case in time_entry.get("cases", []):
                            records.append({
                                "date":       hearing_date,
                                "hl_code":    hl_code,
                                "location":   normalized_location,
                                "courtroom":  courtroom,
                                "session_time": session_time,
                                "case_num":   case.get("case_num", ""),
                                "case_title": case.get("case_title", ""),
                                "case_type":  case.get("case_type", ""),
                                "argument_time": case.get(
                                    "argument_time", ""
                                ),
                                "originating_district": case.get(
                                    "originating_district", ""
                                ),
                            })

        logger.info(
            "[C9] Flattened %d case records from %d date entries",
            len(records), len(sittings),
        )
        return records

    # -----------------------------------------------------------------------
    # Phase 2: Fetch synopses + judges from Lambda API
    # -----------------------------------------------------------------------

    def _get_unique_sittings(self, records: List[Dict]) -> List[tuple]:
        """Get unique (date, hl_code) pairs from flat records."""
        seen = set()
        sittings = []
        for r in records:
            key = (r.get("date", ""), r.get("hl_code", ""))
            if key[0] and key[1] and key not in seen:
                seen.add(key)
                sittings.append(key)
        return sorted(sittings)

    def _fetch_all_sitting_details(
        self,
        sittings: List[tuple],
        progress_callback: Optional[Callable] = None,
    ) -> Dict[str, Dict[str, str]]:
        """
        For each (date, hl_code) pair, call the Lambda API and extract
        synopsis + formatted_panel for each case.

        Lambda API URL:
            https://avessdwb2hgbkch6hmpylkkcy40feibd.lambda-url.us-west-2.on.aws/
            ?date=YYYY-MM-DD&loc=XX

        Response structure:
            {
              "courthouse": { ... },
              "panel_sittings": [
                {
                  "courtroom": "...",
                  "times": [
                    {
                      "time": "9:00 AM",
                      "cases": [
                        {
                          "case_num": "25-6308",
                          "case_title": "Flores v. Blanche, et al.",
                          "synopsis": "Government defendants appeal...",
                          "formatted_panel": "MURGUIA, WARDLAW, ...",
                          "case_type": "Civil",
                          "argument_time": "30 min/side",
                          ...
                        }
                      ]
                    }
                  ]
                }
              ]
            }

        Returns:
            Dict mapping case_number → {
                "synopsis": "...",
                "judges": "...",
            }
        """
        case_details = {}
        total = len(sittings)

        for idx, (date_str, loc_code) in enumerate(sittings, 1):
            if progress_callback:
                progress_callback(
                    "details",
                    f"Fetching details: {date_str} / {loc_code} "
                    f"({idx}/{total})",
                    idx,
                    total,
                )

            url = f"{C9_LAMBDA_API}?date={date_str}&loc={loc_code}"
            data = self._get_json(url)

            if not data:
                logger.warning(
                    "[C9] No data from Lambda API for %s/%s",
                    date_str, loc_code,
                )
                continue

            # Parse the panel_sittings from the API response
            panel_sittings = data.get("panel_sittings", [])

            for courtroom_entry in panel_sittings:
                for time_entry in courtroom_entry.get("times", []):
                    for case in time_entry.get("cases", []):
                        case_num = case.get("case_num", "").strip()
                        if not case_num:
                            continue

                        synopsis = case.get("synopsis", "").strip()
                        judges = case.get(
                            "formatted_panel", ""
                        ).strip()

                        case_details[case_num] = {
                            "synopsis": synopsis,
                            "judges":   judges,
                        }

            api_count = sum(
                1 for cd in case_details.values() if cd.get("synopsis")
            )
            logger.info(
                "[C9] API %s/%s: %d cases extracted "
                "(%d total with synopses so far)",
                date_str, loc_code,
                len(panel_sittings), api_count,
            )

        total_with_synopsis = sum(
            1 for cd in case_details.values() if cd.get("synopsis")
        )
        total_with_judges = sum(
            1 for cd in case_details.values() if cd.get("judges")
        )
        logger.info(
            "[C9] Lambda API complete: %d cases, "
            "%d with synopses, %d with judges",
            len(case_details), total_with_synopsis, total_with_judges,
        )
        return case_details

    # -----------------------------------------------------------------------
    # Normalization helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _normalize_location(hl_code: str, display_name: str) -> str:
        """Normalize location to 'City, ST' format."""
        if hl_code and hl_code in C9_LOCATION_MAP:
            return C9_LOCATION_MAP[hl_code]
        if display_name:
            key = display_name.strip().lower()
            if key in C9_DISPLAY_NAME_MAP:
                return C9_DISPLAY_NAME_MAP[key]
            return display_name.strip()
        return ""

    @staticmethod
    def _format_date(raw_date: str) -> str:
        """Ensure date is in YYYY-MM-DD format."""
        if not raw_date:
            return ""
        raw_date = raw_date.strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", raw_date):
            return raw_date
        for fmt in ("%B %d, %Y", "%B %d %Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(raw_date, fmt).strftime(
                    "%Y-%m-%d"
                )
            except ValueError:
                continue
        return raw_date

    @staticmethod
    def _normalize_time(raw_time: str) -> str:
        """Normalize time to consistent 'H:MM AM/PM' format."""
        if not raw_time:
            return ""
        raw_time = raw_time.strip()
        for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
            try:
                parsed = datetime.strptime(raw_time, fmt)
                return parsed.strftime("%-I:%M %p")
            except ValueError:
                continue
        return raw_time

    @staticmethod
    def _derive_purpose(argument_time: str) -> str:
        """Derive Purpose of Hearing from the argument_time field."""
        if not argument_time:
            return ""
        at = argument_time.strip().lower()
        if at.startswith("subm"):
            return "Submitted on Briefs"
        if at.startswith("def"):
            return "Submission Deferred"
        if re.match(r"\d+\s*min", at):
            return "Oral Argument"
        return "Oral Argument"

    def _to_standard_schema(
        self,
        records: List[Dict],
        case_details: Dict[str, Dict[str, str]],
    ) -> List[Dict]:
        """
        Map flat records to the standard 11-field output schema,
        merging in synopses and judges from the Lambda API.

        Output keys (snake_case — matches Circuit 1 reference):
          date, case_number, case_name, nature_of_case, court_name,
          location, judges_panel, courtroom, purpose_of_hearing,
          time, description
        """
        normalized = []
        for r in records:
            case_num = r.get("case_num", "").strip()

            # Look up details from Lambda API
            details = case_details.get(case_num, {})
            synopsis = details.get("synopsis", "")
            judges = details.get("judges", "")

            normalized.append({
                "date":               self._format_date(
                                          r.get("date", "")
                                      ),
                "case_number":        case_num,
                "case_name":          r.get("case_title", "").strip(),
                "nature_of_case":     r.get("case_type", "").strip(),
                "court_name":         C9_COURT_NAME,
                "location":           r.get("location", "").strip(),
                "judges_panel":       judges,
                "courtroom":          r.get("courtroom", "").strip(),
                "purpose_of_hearing": self._derive_purpose(
                                          r.get("argument_time", "")
                                      ),
                "time":               self._normalize_time(
                                          r.get("session_time", "")
                                      ),
                "description":        synopsis,
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
        Main entry point. Scrapes the Ninth Circuit oral argument calendar.

        Two-phase approach:
          Phase 1: Extract case metadata from global_panel_sittings JSON
                   (single request to /cases/calendar/)
          Phase 2: Fetch case synopses + judges from Lambda API
                   (one request per date+location combination)

        Returns a list of dicts in the standard 11-field schema
        (snake_case keys matching Circuit 1 reference format).
        """
        # ── Phase 1: Fetch calendar page and extract JSON ──
        if progress_callback:
            progress_callback("fetch", "Fetching calendar page...", 0, 5)

        html = self._fetch_calendar_html()
        if not html:
            logger.error("[C9] Failed to fetch calendar page")
            if progress_callback:
                progress_callback(
                    "fetch", "Failed to fetch calendar page", 1, 5
                )
            return []

        logger.info("[C9] Fetched calendar page (%d bytes)", len(html))

        if progress_callback:
            progress_callback(
                "extract", "Extracting hearing data...", 1, 5
            )

        sittings = self._extract_panel_sittings(html)
        if not sittings:
            logger.error("[C9] No sitting data found in page")
            if progress_callback:
                progress_callback("extract", "No data found", 2, 5)
            return []

        # ── Phase 1: Flatten ──
        if progress_callback:
            progress_callback("flatten", "Processing cases...", 2, 5)

        flat_records = self._flatten_sittings(sittings)

        # ── Phase 2: Fetch synopses + judges from Lambda API ──
        case_details = {}
        if self.fetch_descriptions and flat_records:
            unique_sittings = self._get_unique_sittings(flat_records)
            total_sittings = len(unique_sittings)

            if progress_callback:
                progress_callback(
                    "details",
                    f"Fetching synopses from {total_sittings} "
                    f"sitting pages via API...",
                    3, 5,
                )

            if total_sittings > 0:
                def detail_progress(stage, label, current, total):
                    if progress_callback and total > 0:
                        sub_pct = current / total
                        overall = 3 + sub_pct
                        progress_callback("details", label, overall, 5)

                case_details = self._fetch_all_sitting_details(
                    unique_sittings,
                    progress_callback=detail_progress,
                )
        else:
            if progress_callback:
                progress_callback(
                    "details",
                    "Skipping synopsis fetch (disabled or no records)",
                    4, 5,
                )

        # ── Normalize to standard schema ──
        if progress_callback:
            progress_callback(
                "normalize", "Normalizing to standard schema...", 4, 5
            )

        normalized = self._to_standard_schema(flat_records, case_details)

        # ── Deduplicate by (Case Number, Date) ──
        seen = set()
        unique = []
        for c in normalized:
            key = (c["case_number"], c["date"])
            if key not in seen:
                seen.add(key)
                unique.append(c)

        # Stats
        with_desc = sum(1 for c in unique if c.get("description"))
        with_judges = sum(1 for c in unique if c.get("judges_panel"))
        logger.info(
            "[C9] Final: %d cases (%d with synopses, %d with judges, "
            "%d before dedup)",
            len(unique), with_desc, with_judges, len(normalized),
        )

        if progress_callback:
            progress_callback(
                "done",
                f"Complete — {len(unique)} cases "
                f"({with_desc} with synopses, "
                f"{with_judges} with judges)",
                5, 5,
            )

        return unique

    # Aliases for compatibility
    scrape_current = scrape
    scrape_all = scrape

    def scrape_sitting_url(self, url: str) -> List[Dict]:
        """Scrape a specific day-detail page."""
        resp = self._get(url)
        if not resp:
            return []
        sittings = self._extract_panel_sittings(resp.text)
        if sittings:
            flat = self._flatten_sittings(sittings)
            return self._to_standard_schema(flat, {})
        return []


# ---------------------------------------------------------------------------
# CLI entry point for testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    scraper = USCA9Scraper()

    print("=" * 70)
    print("NINTH CIRCUIT SCRAPER — Two-Phase (JSON + Lambda API)")
    print("=" * 70)

    def cli_progress(stage, label, current, total):
        pct = (current / total * 100) if total else 0
        print(f"  [{stage}] {pct:5.1f}% — {label}")

    cases = scraper.scrape(progress_callback=cli_progress)

    print(f"\nTotal cases: {len(cases)}")

    if cases:
        print("\n--- Sample Cases ---")
        for i, c in enumerate(cases[:5], 1):
            print(f"\nCase {i}:")
            for k, v in c.items():
                display_v = (
                    v[:100] + "..." if len(str(v)) > 100 else v
                )
                print(f"  {k:20s}: {display_v}")

        print("\n--- Field Completeness ---")
        fields = [
            "date", "case_number", "case_name", "nature_of_case",
            "court_name", "location", "judges_panel", "courtroom",
            "purpose_of_hearing", "time", "description",
        ]
        for field in fields:
            filled = sum(1 for c in cases if c.get(field))
            pct = (filled / len(cases) * 100) if cases else 0
            bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
            print(
                f"  {field:20s}: {bar} "
                f"{filled}/{len(cases)} ({pct:.0f}%)"
            )

        with_desc = [c for c in cases if c.get("description")]
        print(
            f"\n--- Cases with Synopses: "
            f"{len(with_desc)}/{len(cases)} ---"
        )
        for c in with_desc[:5]:
            print(f"\n  {c['case_number']}: {c['case_name']}")
            desc = c["description"]
            print(
                f"  Synopsis: "
                f"{desc[:150]}{'...' if len(desc) > 150 else ''}"
            )

        with_judges = [c for c in cases if c.get("judges_panel")]
        print(
            f"\n--- Cases with Judges: "
            f"{len(with_judges)}/{len(cases)} ---"
        )
        for c in with_judges[:5]:
            print(
                f"  {c['case_number']}: {c['judges_panel'][:80]}"
            )
