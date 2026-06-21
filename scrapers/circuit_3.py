"""
Third Circuit Court of Appeals — Oral Argument Calendar Scraper.

The Third Circuit portal (ca03portal.powerappsportals.us) is a Microsoft
PowerApps portal that renders case data via JavaScript.

CRITICAL: On Windows, Playwright CANNOT run inside Streamlit's event loop
due to asyncio SelectorEventLoop vs ProactorEventLoop conflict.
Solution: We run Playwright in a SEPARATE subprocess via subprocess.Popen,
which gets its own event loop and avoids the conflict entirely.

Architecture:
  1. Discover hearing dates from ca3.uscourts.gov/calendar
  2. For each date, spawn a subprocess that uses Playwright to render the page
  3. The subprocess saves the rendered HTML to a temp file
  4. The main process reads the HTML and parses tables with BeautifulSoup
  5. Normalize to standard 11-field schema

Standard 11-field output schema:
  Date | Case Number | Case Name | Nature of Case | Court Name |
  Location | Judges / Panel | Courtroom | Purpose of Hearing |
  Time | Description
"""

import re
import ssl
import sys
import os
import json
import time
import logging
import tempfile
import threading
import subprocess
from datetime import datetime
from typing import List, Dict, Optional, Callable
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────
C3_COURT_NAME = "United States Court of Appeals for the Third Circuit"
C3_DEFAULT_LOCATION = "Philadelphia, PA"
C3_BASE_URL = "https://www.ca3.uscourts.gov"
C3_CALENDAR_URL = f"{C3_BASE_URL}/calendar"
C3_PORTAL_BASE = "https://ca03portal.powerappsportals.us"

C3_LANDING_URL_TEMPLATE = f"{C3_PORTAL_BASE}/Oral-Argument-Landing?hrngDt={{date}}"
C3_SUMMARY_URL_TEMPLATE = (
    f"{C3_PORTAL_BASE}/Oral-Argument-Summary"
    "?id=b5b7771c-bf4e-f111-bec5-001dd83042d7&hrngDt={{date}}"
)

C3_RE_CASE_NUM = re.compile(r'\d{2}-\d{1,5}')
C3_RE_LOC_TIME = re.compile(r'^(.+?)\s*-\s*(.+?)\s*/\s*(.+)$')

C3_JUNK_PATTERNS = [
    "sort ascending", "sort descending", "there are no records",
    "you don't have permissions", "error completing", "loading...",
    "case number", "caption",
]


# ─────────────────────────────────────────────
# Playwright Subprocess Script
# This script is written to a temp file and executed
# in a separate Python process to avoid asyncio conflicts.
# ─────────────────────────────────────────────
PLAYWRIGHT_SCRIPT = '''
"""Subprocess script: render a PowerApps portal page with Playwright."""
import sys
import json
import time

def main():
    url = sys.argv[1]
    output_file = sys.argv[2]
    tab_name = sys.argv[3] if len(sys.argv) > 3 else "Argued"

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        result = {"error": "Playwright not installed", "html": "", "submitted_html": ""}
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(result, f)
        return

    result = {"error": "", "html": "", "submitted_html": ""}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                ignore_https_errors=True,
            )
            page = context.new_page()

            # Navigate to the page
            page.goto(url, wait_until="networkidle", timeout=60000)

            # Wait for table to appear
            try:
                page.wait_for_selector("table", timeout=20000)
            except Exception:
                time.sleep(5)

            # Extra wait for data to populate
            time.sleep(3)

            # Get the Argued tab HTML
            result["html"] = page.content()

            # Try to click the Submitted tab
            submitted_selectors = [
                "a:has-text('Submitted')",
                "button:has-text('Submitted')",
                "li:has-text('Submitted') a",
                ".nav-link:has-text('Submitted')",
                "[data-name='Submitted']",
            ]

            for selector in submitted_selectors:
                try:
                    el = page.query_selector(selector)
                    if el and el.is_visible():
                        el.click()
                        time.sleep(3)
                        try:
                            page.wait_for_selector("table tbody tr", timeout=10000)
                        except Exception:
                            time.sleep(2)
                        result["submitted_html"] = page.content()
                        break
                except Exception:
                    continue

            browser.close()

    except Exception as e:
        result["error"] = str(e)

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f)


if __name__ == "__main__":
    main()
'''


# ─────────────────────────────────────────────
# SSL Adapter
# ─────────────────────────────────────────────
class _SSLAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        except Exception:
            pass
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)

    def send(self, request, **kwargs):
        kwargs["verify"] = False
        return super().send(request, **kwargs)


def _make_session():
    s = requests.Session()
    s.verify = False
    s.mount("https://", _SSLAdapter())
    s.mount("http://", HTTPAdapter())
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    })
    return s


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def _is_junk(text: str) -> bool:
    if not text or not text.strip():
        return True
    lower = text.strip().lower()
    for pat in C3_JUNK_PATTERNS:
        if pat in lower:
            return True
    return False


def _parse_location_time(raw: str) -> Dict[str, str]:
    result = {"location": "", "courtroom": "", "time": ""}
    if not raw or not raw.strip():
        return result

    raw = raw.strip()
    m = C3_RE_LOC_TIME.match(raw)
    if m:
        result["location"] = m.group(1).strip()
        result["courtroom"] = m.group(2).strip()
        result["time"] = m.group(3).strip()
    elif " - " in raw:
        parts = raw.split(" - ", 1)
        result["location"] = parts[0].strip()
        remainder = parts[1].strip()
        if "/" in remainder:
            idx = remainder.rfind("/")
            result["courtroom"] = remainder[:idx].strip().strip("/").strip()
            result["time"] = remainder[idx + 1:].strip()
        else:
            result["courtroom"] = remainder
    else:
        result["location"] = raw

    return result


def _extract_case_numbers(text: str) -> str:
    nums = C3_RE_CASE_NUM.findall(text)
    return ", ".join(nums) if nums else ""


# ─────────────────────────────────────────────
# HTML Table Parser
# ─────────────────────────────────────────────
def parse_c3_html_tables(html: str, hearing_date: str, tab_name: str = "Argued") -> List[Dict]:
    """
    Parse rendered portal HTML for case data.

    Argued tab columns:  Case Number | Caption | Panel | Location / Time | Video
    Submitted tab columns: Case Number | Caption | Panel
    """
    soup = BeautifulSoup(html, "html.parser")
    all_cases = []

    tables = soup.find_all("table")
    logger.info(f"C3 Parser: Found {len(tables)} table(s) for {hearing_date} ({tab_name})")

    for table_idx, table in enumerate(tables):
        # Determine table type from headers
        headers = []
        thead = table.find("thead")
        if thead:
            for th in thead.find_all("th"):
                headers.append(th.get_text(strip=True).lower())

        # Determine if this is Argued or Submitted based on columns
        has_location = any("location" in h for h in headers)
        has_video = any("video" in h for h in headers)

        # Determine effective tab name
        effective_tab = tab_name
        if has_location or has_video:
            effective_tab = "Argued"
        elif headers and not has_location and not has_video:
            # Check if only 3 columns (Case Number, Caption, Panel)
            data_headers = [h for h in headers if h and h not in ("", " ")]
            if len(data_headers) <= 3:
                effective_tab = "Submitted"

        # Also check surrounding context
        prev = table.find_previous(["h1", "h2", "h3", "h4", "h5", "h6", "caption", "div"])
        if prev:
            prev_text = prev.get_text(strip=True).lower()
            if "submitted" in prev_text:
                effective_tab = "Submitted"
            elif "argued" in prev_text:
                effective_tab = "Argued"

        # Parse rows
        tbody = table.find("tbody")
        rows = tbody.find_all("tr") if tbody else table.find_all("tr")

        for row in rows:
            cells = row.find_all("td")
            if not cells:
                continue

            texts = [re.sub(r'\s+', ' ', cell.get_text(strip=True)) for cell in cells]

            if not texts or len(texts) < 2:
                continue

            # First cell should contain a case number
            case_number = _extract_case_numbers(texts[0])
            if not case_number:
                continue

            # Skip junk rows
            if _is_junk(texts[0]) or _is_junk(texts[1]):
                continue

            # Extract fields
            caption = texts[1] if len(texts) > 1 else ""
            panel = ""
            location_time_raw = ""

            if effective_tab == "Argued":
                panel = texts[2] if len(texts) > 2 else ""
                location_time_raw = texts[3] if len(texts) > 3 else ""
            else:
                panel = texts[2] if len(texts) > 2 else ""

            if _is_junk(panel):
                panel = ""

            loc_info = _parse_location_time(location_time_raw)

            case = {
                "case_number": case_number,
                "case_name": caption,
                "hearing_date": hearing_date,
                "panel": panel,
                "location": loc_info["location"],
                "courtroom": loc_info["courtroom"],
                "time": loc_info["time"],
                "tab": effective_tab,
            }
            all_cases.append(case)

    logger.info(f"C3 Parser: Extracted {len(all_cases)} cases for {hearing_date} ({tab_name})")
    return all_cases


# ─────────────────────────────────────────────
# Subprocess-based Playwright Renderer
# ─────────────────────────────────────────────
def render_portal_via_subprocess(url: str, timeout: int = 90) -> Dict[str, str]:
    """
    Spawn a separate Python process to render the portal page with Playwright.
    Returns dict with 'html', 'submitted_html', 'error' keys.

    This avoids the Streamlit + Windows asyncio event loop conflict entirely.
    """
    # Write the Playwright script to a temp file
    script_fd, script_path = tempfile.mkstemp(suffix=".py", prefix="c3_pw_")
    output_fd, output_path = tempfile.mkstemp(suffix=".json", prefix="c3_out_")

    try:
        # Write script
        with os.fdopen(script_fd, "w", encoding="utf-8") as f:
            f.write(PLAYWRIGHT_SCRIPT)

        # Close the output fd so the subprocess can write to it
        os.close(output_fd)

        # Run the script in a separate process
        logger.info(f"C3: Spawning Playwright subprocess for {url}")
        proc = subprocess.Popen(
            [sys.executable, script_path, url, output_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            logger.error(f"C3: Playwright subprocess timed out after {timeout}s")
            return {"html": "", "submitted_html": "", "error": "Subprocess timed out"}

        if proc.returncode != 0:
            err_msg = stderr.decode("utf-8", errors="replace")[:500]
            logger.error(f"C3: Playwright subprocess failed (rc={proc.returncode}): {err_msg}")
            return {"html": "", "submitted_html": "", "error": err_msg}

        # Read the output
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                result = json.load(f)
            return result
        except Exception as e:
            logger.error(f"C3: Failed to read subprocess output: {e}")
            return {"html": "", "submitted_html": "", "error": str(e)}

    finally:
        # Clean up temp files
        try:
            os.unlink(script_path)
        except Exception:
            pass
        try:
            os.unlink(output_path)
        except Exception:
            pass


# ─────────────────────────────────────────────
# Main Scraper Class
# ─────────────────────────────────────────────
class CA3CourtScraper:
    BASE_URL = C3_BASE_URL
    CALENDAR_URL = C3_CALENDAR_URL

    def __init__(self, timeout=30, **kwargs):
        self.timeout = timeout
        self.session = _make_session()
        self._raw_data = []
        self._debug_html = {}

    # ── Discover hearing dates ──
    def discover_hearing_dates(self, progress_callback=None) -> List[str]:
        dates = set()

        urls_to_try = [
            self.CALENDAR_URL,
            f"{self.CALENDAR_URL}/month",
        ]

        for url in urls_to_try:
            try:
                resp = self.session.get(url, timeout=self.timeout)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "html.parser")

                for a_tag in soup.find_all("a", href=True):
                    href = a_tag["href"].strip()

                    if "hrngDt=" in href:
                        parsed = urlparse(urljoin(url, href))
                        params = parse_qs(parsed.query)
                        dt = params.get("hrngDt", [None])[0]
                        if dt:
                            try:
                                datetime.strptime(dt, "%Y-%m-%d")
                                dates.add(dt)
                            except ValueError:
                                pass

                    if "case-list" in href.lower() or "content/case-list" in href.lower():
                        try:
                            cl_url = urljoin(url, href)
                            cl_resp = self.session.get(cl_url, timeout=self.timeout)
                            cl_resp.raise_for_status()
                            cl_soup = BeautifulSoup(cl_resp.text, "html.parser")
                            for cl_a in cl_soup.find_all("a", href=True):
                                cl_href = cl_a["href"].strip()
                                if "hrngDt=" in cl_href:
                                    cl_parsed = urlparse(urljoin(cl_url, cl_href))
                                    cl_params = parse_qs(cl_parsed.query)
                                    cl_dt = cl_params.get("hrngDt", [None])[0]
                                    if cl_dt:
                                        try:
                                            datetime.strptime(cl_dt, "%Y-%m-%d")
                                            dates.add(cl_dt)
                                        except ValueError:
                                            pass
                        except Exception as e:
                            logger.debug(f"C3: Error fetching case-list {href}: {e}")

            except Exception as e:
                logger.warning(f"C3: Error fetching {url}: {e}")

        sorted_dates = sorted(dates)
        logger.info(f"C3: Discovered {len(sorted_dates)} hearing dates: {sorted_dates}")
        return sorted_dates

    # ── Scrape a single hearing date ──
    def scrape_hearing_date(self, hearing_date: str) -> List[Dict]:
        all_cases = []

        # Try both URL patterns
        urls_to_try = [
            ("Landing", C3_LANDING_URL_TEMPLATE.format(date=hearing_date)),
            ("Summary", C3_SUMMARY_URL_TEMPLATE.format(date=hearing_date)),
        ]

        for url_label, url in urls_to_try:
            try:
                logger.info(f"C3: Rendering {url_label} page for {hearing_date} via subprocess")

                result = render_portal_via_subprocess(url, timeout=90)

                if result.get("error"):
                    logger.warning(f"C3: Subprocess error for {url_label}/{hearing_date}: {result['error']}")

                # Parse Argued tab HTML
                argued_html = result.get("html", "")
                if argued_html:
                    self._debug_html[f"{hearing_date}_{url_label}_argued"] = argued_html[:10000]
                    argued_cases = parse_c3_html_tables(argued_html, hearing_date, "Argued")
                    all_cases.extend(argued_cases)

                # Parse Submitted tab HTML
                submitted_html = result.get("submitted_html", "")
                if submitted_html:
                    self._debug_html[f"{hearing_date}_{url_label}_submitted"] = submitted_html[:10000]
                    submitted_cases = parse_c3_html_tables(submitted_html, hearing_date, "Submitted")
                    all_cases.extend(submitted_cases)

                if all_cases:
                    logger.info(f"C3: {url_label} yielded {len(all_cases)} cases for {hearing_date}")
                    break  # Got data, no need to try the other URL
                else:
                    logger.info(f"C3: {url_label} yielded 0 cases for {hearing_date}, trying next URL")

            except Exception as e:
                logger.error(f"C3: Error scraping {url_label} for {hearing_date}: {e}")

        return all_cases

    # ── Main entry point ──
    def scrape_all(self, progress_callback: Optional[Callable] = None) -> List[Dict]:
        all_raw = []

        # Step 1: Discover dates
        if progress_callback:
            progress_callback("calendar", "Scanning calendar...", 0, 1)

        dates = self.discover_hearing_dates(progress_callback)

        if not dates:
            logger.warning("C3: No hearing dates found")
            if progress_callback:
                progress_callback("calendar", "No dates found", 1, 1)
            return []

        if progress_callback:
            progress_callback("calendar", f"Found {len(dates)} dates", 1, 1)

        # Step 2: Scrape each date
        total = len(dates)
        for idx, dt in enumerate(dates):
            if progress_callback:
                progress_callback("portal", f"Scraping {dt}", idx + 1, total)

            cases = self.scrape_hearing_date(dt)
            all_raw.extend(cases)
            time.sleep(1)

        # Step 3: Deduplicate
        seen = set()
        unique = []
        for c in all_raw:
            key = f"{c.get('case_number', '')}|{c.get('hearing_date', '')}|{c.get('tab', '')}"
            if key not in seen and c.get("case_number"):
                seen.add(key)
                unique.append(c)

        self._raw_data = unique
        logger.info(f"C3: Total unique cases: {len(unique)}")

        # Step 4: Normalize
        return normalize_c3_cases(unique)

    def get_raw_data(self):
        return self._raw_data

    def get_debug_html(self):
        return self._debug_html


# ─────────────────────────────────────────────
# Normalize to standard 11-field schema
# ─────────────────────────────────────────────
def normalize_c3_cases(raw_cases: List[Dict]) -> List[Dict]:
    normalized = []

    for c in raw_cases:
        case_number = c.get("case_number", "").strip()
        if not case_number:
            continue

        hearing_date = c.get("hearing_date", "")
        formatted_date = hearing_date
        if hearing_date:
            for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y"):
                try:
                    formatted_date = datetime.strptime(hearing_date, fmt).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue

        tab = c.get("tab", "Argued")
        purpose = "Submitted" if tab == "Submitted" else "Oral Argument"

        location = c.get("location", "").strip()
        if not location:
            location = C3_DEFAULT_LOCATION

        normalized.append({
            "Date": formatted_date,
            "Case Number": case_number,
            "Case Name": c.get("case_name", "").strip(),
            "Nature of Case": "",
            "Court Name": C3_COURT_NAME,
            "Location": location,
            "Judges / Panel": c.get("panel", "").strip(),
            "Courtroom": c.get("courtroom", "").strip(),
            "Purpose of Hearing": purpose,
            "Time": c.get("time", "").strip(),
            "Description": "",
        })

    return normalized


# ─────────────────────────────────────────────
# Thread runner (for Streamlit)
# ─────────────────────────────────────────────
def run_c3_scraper_in_thread(scraper, start_date=None, end_date=None,
                              status_placeholder=None, progress_bar=None):
    result = None
    exception = None
    progress_state = {
        "stage": "starting", "msg": "", "current": 0, "total": 0, "done": False,
    }

    def _target():
        nonlocal result, exception
        try:
            def _progress(stage, msg, current, total):
                progress_state["stage"] = stage
                progress_state["msg"] = msg
                progress_state["current"] = current
                progress_state["total"] = total

            result = scraper.scrape_all(progress_callback=_progress)
        except Exception as e:
            exception = e
        finally:
            progress_state["done"] = True

    t = threading.Thread(target=_target)
    t.start()

    while t.is_alive():
        stage = progress_state["stage"]
        msg = progress_state["msg"]
        current = progress_state["current"]
        total = progress_state["total"]

        if status_placeholder:
            if stage == "calendar":
                status_placeholder.info(f"📅 {msg}")
            elif stage == "portal":
                status_placeholder.info(f"🌐 {msg} ({current}/{total})")
            else:
                status_placeholder.info(f"⏳ {msg}")

        if progress_bar and total > 0:
            progress_bar.progress(min(current / total, 1.0))

        time.sleep(0.5)

    t.join()

    if exception:
        raise exception

    return result