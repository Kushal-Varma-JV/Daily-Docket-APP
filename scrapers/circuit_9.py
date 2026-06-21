"""
Scraper for the United States Court of Appeals for the Ninth Circuit.

The Ninth Circuit's new website (launched 2026) embeds all oral argument
calendar data as a JavaScript variable `global_panel_sittings` directly
in the HTML source of /cases/calendar/. The data is a JSON array with
the structure:

    global_panel_sittings = [
        {
            date: "2026-06-02",
            locations: [
                {
                    hl_code: "SE",
                    location_display_name: "Seattle",
                    courtrooms: [
                        {
                            courtroom: "7th Floor Courtroom 2",
                            times: [
                                {
                                    time: "9:00 am",
                                    cases: [
                                        {
                                            case_num: "25-1234",
                                            case_title: "Smith v. Jones",
                                            case_type: "Civil",
                                            argument_time: "15 min",
                                            originating_district: "W. WA"
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }
    ]

Strategy:
    1. GET /cases/calendar/
    2. Extract `global_panel_sittings` JSON from <script> tag via regex
    3. Flatten the nested structure into 11-field standard records

No Playwright, no API probing, no HTML table parsing needed.
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

# Regex to extract the global_panel_sittings JSON from the page source.
# The variable is assigned in a <script> tag as:
#   let global_panel_sittings=[...];
C9_RE_PANEL_SITTINGS = re.compile(
    r"let\s+global_panel_sittings\s*=\s*(\[.*?\])\s*;?\s*(?:console\.log|let\s|var\s|function\s|</script>)",
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

# Display name fallback mapping (if hl_code not in map above)
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
# SSL Bypass Adapter (Ninth Circuit sometimes has cert issues)
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
    ):
        self.base_url = base_url.rstrip("/")
        self.verify_ssl = verify_ssl
        self.request_delay = request_delay
        self.max_retries = max_retries
        self.timeout = timeout

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
    # HTTP
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

        logger.error("[C9] All %d attempts failed for %s", self.max_retries, url)
        return None

    # -----------------------------------------------------------------------
    # Extraction: get JSON from page source
    # -----------------------------------------------------------------------

    def _fetch_calendar_html(self) -> Optional[str]:
        """Fetch the calendar page HTML."""
        url = f"{self.base_url}{C9_CALENDAR_PATH}"
        resp = self._get(url)
        if not resp:
            return None
        return resp.text

    def _extract_panel_sittings(self, html: str) -> Optional[List[Dict]]:
        """
        Extract the global_panel_sittings JSON array from the page HTML.

        The data is embedded in a <script> tag as:
            let global_panel_sittings = [...];
        """
        # Try primary pattern
        m = C9_RE_PANEL_SITTINGS.search(html)
        if not m:
            # Try fallback
            m = C9_RE_PANEL_SITTINGS_FALLBACK.search(html)

        if not m:
            logger.error("[C9] Could not find global_panel_sittings in page source")
            return None

        raw_json = m.group(1)

        # The JSON uses unquoted keys (JavaScript object notation).
        # We need to convert it to valid JSON by quoting the keys.
        # Pattern: word characters followed by colon (but not inside strings)
        valid_json = self._js_object_to_json(raw_json)

        try:
            data = json.loads(valid_json)
            logger.info("[C9] Extracted %d date entries from global_panel_sittings", len(data))
            return data
        except json.JSONDecodeError as exc:
            logger.error("[C9] Failed to parse global_panel_sittings JSON: %s", exc)
            # Try a more aggressive cleanup
            try:
                # Remove trailing commas before ] or }
                cleaned = re.sub(r",\s*([}\]])", r"\1", valid_json)
                data = json.loads(cleaned)
                logger.info("[C9] Parsed after trailing comma cleanup: %d entries", len(data))
                return data
            except json.JSONDecodeError as exc2:
                logger.error("[C9] Second parse attempt also failed: %s", exc2)
                return None

    @staticmethod
    def _js_object_to_json(js_text: str) -> str:
        """
        Convert JavaScript object notation to valid JSON.

        Handles:
          - Unquoted keys:  {date: "2026-06-02"}  → {"date": "2026-06-02"}
          - Single-quoted strings (rare but possible)
          - Trailing commas
        """
        result = []
        i = 0
        in_string = False
        string_char = None

        while i < len(js_text):
            ch = js_text[i]

            # Handle string boundaries
            if in_string:
                result.append(ch)
                if ch == "\\" and i + 1 < len(js_text):
                    # Escaped character — append next char too
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
                # Convert single quotes to double quotes
                result.append('"' if ch == "'" else ch)
                i += 1
                continue

            # Outside strings: look for unquoted keys
            # Pattern: start of key position (after { or ,) followed by
            # word chars and then a colon
            if ch.isalpha() or ch == "_":
                # Collect the full identifier
                j = i
                while j < len(js_text) and (js_text[j].isalnum() or js_text[j] == "_"):
                    j += 1
                identifier = js_text[i:j]

                # Skip whitespace after identifier
                k = j
                while k < len(js_text) and js_text[k] in (" ", "\t", "\n", "\r"):
                    k += 1

                if k < len(js_text) and js_text[k] == ":":
                    # This is an unquoted key — quote it
                    result.append('"')
                    result.append(identifier)
                    result.append('"')
                    i = j
                    continue
                else:
                    # Not a key — could be a value like true/false/null
                    result.append(ch)
                    i += 1
                    continue

            result.append(ch)
            i += 1

        text = "".join(result)
        # Remove trailing commas before ] or }
        text = re.sub(r",\s*([}\]])", r"\1", text)
        return text

    # -----------------------------------------------------------------------
    # Flattening: nested JSON → flat case records
    # -----------------------------------------------------------------------

    def _flatten_sittings(self, sittings: List[Dict]) -> List[Dict]:
        """
        Flatten the nested global_panel_sittings structure into a flat
        list of case records.

        Input hierarchy:
            sittings[].date
            sittings[].locations[].location_display_name
            sittings[].locations[].hl_code
            sittings[].locations[].courtrooms[].courtroom
            sittings[].locations[].courtrooms[].times[].time
            sittings[].locations[].courtrooms[].times[].cases[].case_num
            sittings[].locations[].courtrooms[].times[].cases[].case_title
            sittings[].locations[].courtrooms[].times[].cases[].case_type
            sittings[].locations[].courtrooms[].times[].cases[].argument_time
            sittings[].locations[].courtrooms[].times[].cases[].originating_district

        Output: flat list of dicts with all context fields.
        """
        records = []

        for date_entry in sittings:
            hearing_date = date_entry.get("date", "")

            for location in date_entry.get("locations", []):
                hl_code = location.get("hl_code", "")
                display_name = location.get("location_display_name", "")
                normalized_location = self._normalize_location(hl_code, display_name)

                for courtroom_entry in location.get("courtrooms", []):
                    courtroom = courtroom_entry.get("courtroom", "")

                    for time_entry in courtroom_entry.get("times", []):
                        session_time = time_entry.get("time", "")

                        for case in time_entry.get("cases", []):
                            records.append({
                                "date": hearing_date,
                                "location": normalized_location,
                                "hl_code": hl_code,
                                "courtroom": courtroom,
                                "session_time": session_time,
                                "case_num": case.get("case_num", ""),
                                "case_title": case.get("case_title", ""),
                                "case_type": case.get("case_type", ""),
                                "argument_time": case.get("argument_time", ""),
                                "originating_district": case.get("originating_district", ""),
                            })

        logger.info("[C9] Flattened %d case records from %d date entries",
                    len(records), len(sittings))
        return records

    # -----------------------------------------------------------------------
    # Normalization
    # -----------------------------------------------------------------------

    @staticmethod
    def _normalize_location(hl_code: str, display_name: str) -> str:
        """Normalize location to 'City, ST' format."""
        # Try hl_code first
        if hl_code and hl_code in C9_LOCATION_MAP:
            return C9_LOCATION_MAP[hl_code]

        # Try display name
        if display_name:
            key = display_name.strip().lower()
            if key in C9_DISPLAY_NAME_MAP:
                return C9_DISPLAY_NAME_MAP[key]
            # Return display name as-is if not in map
            return display_name.strip()

        return ""

    @staticmethod
    def _format_date(raw_date: str) -> str:
        """Ensure date is in YYYY-MM-DD format."""
        if not raw_date:
            return ""
        raw_date = raw_date.strip()

        # Already ISO format
        if re.match(r"^\d{4}-\d{2}-\d{2}$", raw_date):
            return raw_date

        # Try other formats
        for fmt in ("%B %d, %Y", "%B %d %Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(raw_date, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue

        return raw_date

    @staticmethod
    def _normalize_time(raw_time: str) -> str:
        """Normalize time to consistent 'H:MM AM/PM' format."""
        if not raw_time:
            return ""
        raw_time = raw_time.strip()

        # Parse and reformat for consistency
        for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
            try:
                parsed = datetime.strptime(raw_time, fmt)
                return parsed.strftime("%-I:%M %p")
            except ValueError:
                continue

        # Return as-is if parsing fails
        return raw_time

    @staticmethod
    def _derive_purpose(argument_time: str) -> str:
        """
        Derive Purpose of Hearing from the argument_time field.

        Values seen in the data:
          - "15 min", "20 min", "30 min", "10 min" → Oral Argument
          - "Subm." → Submitted on Briefs
          - "Def."  → Submission Deferred
        """
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

    @staticmethod
    def _build_description(argument_time: str, originating_district: str) -> str:
        """
        Build a description string from argument_time and
        originating_district.
        """
        parts = []

        if argument_time:
            at = argument_time.strip()
            if at.lower().startswith("subm"):
                parts.append("Submitted on briefs")
            elif at.lower().startswith("def"):
                parts.append("Submission deferred")
            elif re.match(r"\d+\s*min", at, re.IGNORECASE):
                parts.append(f"Argument time: {at}")

        if originating_district:
            parts.append(f"Origin: {originating_district.strip()}")

        return "; ".join(parts)

    def _to_standard_schema(self, records: List[Dict]) -> List[Dict]:
        """
        Map flat records to the standard 11-field output schema.

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
        for r in records:
            normalized.append({
                "Case Name":          r.get("case_title", "").strip(),
                "Case Number":        r.get("case_num", "").strip(),
                "Nature of Case":     r.get("case_type", "").strip(),
                "Court Name":         C9_COURT_NAME,
                "Location":           r.get("location", "").strip(),
                "Judges / Panel":     "",  # Not provided in calendar data
                "Courtroom":          r.get("courtroom", "").strip(),
                "Purpose of Hearing": self._derive_purpose(r.get("argument_time", "")),
                "Date":               self._format_date(r.get("date", "")),
                "Time":               self._normalize_time(r.get("session_time", "")),
                "Description":        self._build_description(
                                          r.get("argument_time", ""),
                                          r.get("originating_district", ""),
                                      ),
            })

        return normalized

    # -----------------------------------------------------------------------
    # Search endpoint (bonus — for targeted lookups)
    # -----------------------------------------------------------------------

    def search_cases(self, query: str) -> Optional[str]:
        """
        Use the calendar search endpoint.
        GET /cases/calendar/calendar-search?input-search-case=<query>

        Returns the HTML response text (for further parsing if needed).
        """
        url = f"{self.base_url}/cases/calendar/calendar-search"
        try:
            resp = self.session.get(
                url,
                params={"input-search-case": query},
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            resp.raise_for_status()
            return resp.text
        except requests.exceptions.RequestException as exc:
            logger.warning("[C9] Search failed for '%s': %s", query, exc)
            return None

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def scrape(
        self,
        progress_callback: Optional[Callable] = None,
    ) -> List[Dict]:
        """
        Main entry point. Scrapes the Ninth Circuit oral argument calendar.

        Returns a list of dicts in the standard 11-field schema.
        """
        # Step 1: Fetch calendar page
        if progress_callback:
            progress_callback("fetch", "Fetching calendar page...", 0, 3)

        html = self._fetch_calendar_html()
        if not html:
            logger.error("[C9] Failed to fetch calendar page")
            if progress_callback:
                progress_callback("fetch", "Failed to fetch calendar page", 1, 3)
            return []

        logger.info("[C9] Fetched calendar page (%d bytes)", len(html))

        # Step 2: Extract JSON data
        if progress_callback:
            progress_callback("extract", "Extracting hearing data...", 1, 3)

        sittings = self._extract_panel_sittings(html)
        if not sittings:
            logger.error("[C9] No sitting data found in page")
            if progress_callback:
                progress_callback("extract", "No data found", 2, 3)
            return []

        # Step 3: Flatten and normalize
        if progress_callback:
            progress_callback("normalize", "Processing cases...", 2, 3)

        flat_records = self._flatten_sittings(sittings)
        normalized = self._to_standard_schema(flat_records)

        # Deduplicate by (Case Number, Date)
        seen = set()
        unique = []
        for c in normalized:
            key = (c["Case Number"], c["Date"])
            if key not in seen:
                seen.add(key)
                unique.append(c)

        logger.info("[C9] Final: %d cases (%d before dedup)",
                    len(unique), len(normalized))

        if progress_callback:
            progress_callback("done", f"Complete — {len(unique)} cases", 3, 3)

        return unique

    # Aliases for compatibility
    scrape_current = scrape
    scrape_all = scrape

    def scrape_sitting_url(self, url: str) -> List[Dict]:
        """
        Scrape a specific day-detail page.
        URL format: /cases/calendar/calendar-day/?date=2026-06-02&loc=SE
        """
        resp = self._get(url)
        if not resp:
            return []

        # The day-detail page likely also embeds data in JS.
        # Try to extract it the same way.
        sittings = self._extract_panel_sittings(resp.text)
        if sittings:
            flat = self._flatten_sittings(sittings)
            return self._to_standard_schema(flat)

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
    print("NINTH CIRCUIT SCRAPER — NEW WEBSITE (Embedded JSON)")
    print("=" * 70)

    def cli_progress(stage, label, current, total):
        pct = (current / total * 100) if total else 0
        print(f"  [{stage}] {pct:5.1f}% — {label}")

    cases = scraper.scrape(progress_callback=cli_progress)

    print(f"\nTotal cases: {len(cases)}")

    if cases:
        # Show first 3 cases
        print("\n--- Sample Cases ---")
        for i, c in enumerate(cases[:3], 1):
            print(f"\nCase {i}:")
            for k, v in c.items():
                print(f"  {k:20s}: {v}")

        # Field completeness
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

        # Stats
        print("\n--- Statistics ---")
        dates = set(c["Date"] for c in cases if c["Date"])
        locations = set(c["Location"] for c in cases if c["Location"])
        purposes = {}
        for c in cases:
            p = c.get("Purpose of Hearing", "")
            purposes[p] = purposes.get(p, 0) + 1

        print(f"  Date range: {min(dates)} to {max(dates)}" if dates else "  No dates")
        print(f"  Locations:  {', '.join(sorted(locations))}")
        print(f"  Purposes:")
        for p, count in sorted(purposes.items(), key=lambda x: -x[1]):
            print(f"    {p or '(empty)':30s}: {count}")