# scrapers/circuit_2.py
"""
Second Circuit (USCA2) oral-argument calendar scraper.
Source: huntcal.com  (via Selenium to bypass human verification)
Schema: 11 fields — the standard 10 + Description.
"""

import re
import ssl
import time
import logging
import urllib3
from datetime import datetime, date
from typing import Optional

import pandas as pd
from bs4 import BeautifulSoup

# Suppress SSL warnings globally
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Selenium helpers
# ---------------------------------------------------------------------------

def _get_driver(verify_ssl: bool = True):
    """Return an undetected-chromedriver instance (headless)."""
    try:
        import undetected_chromedriver as uc
        options = uc.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        if not verify_ssl:
            options.add_argument("--ignore-certificate-errors")
            options.add_argument("--ignore-ssl-errors=yes")
            options.add_argument("--allow-insecure-localhost")
        driver = uc.Chrome(options=options)
        return driver
    except Exception as e:
        logger.warning(f"undetected-chromedriver failed ({e}), falling back to plain selenium")
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        if not verify_ssl:
            options.add_argument("--ignore-certificate-errors")
            options.add_argument("--ignore-ssl-errors=yes")
            options.add_argument("--allow-insecure-localhost")
        driver = webdriver.Chrome(options=options)
        return driver


def _fetch_page(url: str, wait_seconds: int = 5, verify_ssl: bool = True) -> Optional[str]:
    """Fetch a page using Selenium, return HTML string or None."""
    driver = None
    try:
        driver = _get_driver(verify_ssl=verify_ssl)
        driver.get(url)
        time.sleep(wait_seconds)  # let JS/verification resolve
        html = driver.page_source
        return html
    except Exception as e:
        logger.error(f"Selenium fetch failed for {url}: {e}")
        return None
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://www.huntcal.com"
CALENDAR_URL = f"{BASE_URL}/cal/view/USCA2/USCA2"

STANDARD_COLUMNS = [
    "Court Name",
    "Date",
    "Time",
    "Case Number",
    "Case Name",
    "Judges / Panel",
    "Courtroom",
    "Location",
    "Case Link",
    "Nature of Case",
    "Description",
]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_calendar_month(html: str, year: int, month: int) -> list[dict]:
    """Parse the monthly calendar page and return a list of event dicts."""
    soup = BeautifulSoup(html, "html.parser")
    events = []

    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "eventview" not in href and "id=" not in href:
            continue

        title = link.get_text(strip=True)
        if not title:
            continue

        if href.startswith("/"):
            event_url = BASE_URL + href
        elif href.startswith("http"):
            event_url = href
        else:
            event_url = BASE_URL + "/" + href

        events.append({
            "event_title": title,
            "event_url": event_url,
        })

    logger.info(f"Circuit 2: Found {len(events)} events for {year}-{month:02d}")
    return events


def _parse_event_detail(html: str, event_title: str) -> list[dict]:
    """Parse a single event detail page into case rows."""
    soup = BeautifulSoup(html, "html.parser")
    cases = []

    page_text = soup.get_text(" ", strip=True)

    # Date
    event_date = ""
    date_match = re.search(
        r"(\w+day),?\s+(\w+\s+\d{1,2},?\s+\d{4})", page_text
    )
    if date_match:
        raw = date_match.group(2).replace(",", "").strip()
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                event_date = datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                continue

    # Time
    event_time = ""
    time_match = re.search(r"(\d{1,2}:\d{2}\s*[ap]m)", page_text, re.I)
    if time_match:
        event_time = time_match.group(1).strip()

    # Panel / Judges
    panel = ""
    panel_match = re.search(r"Panel:\s*(.+?)(?:\n|$|Cases:)", page_text, re.I)
    if panel_match:
        panel = panel_match.group(1).strip()
        panel = re.split(r"\s{2,}", panel)[0].strip()

    # Courtroom
    courtroom = ""
    cr_match = re.search(r"Courtroom\s+(\S+)", event_title, re.I)
    if cr_match:
        courtroom = "Courtroom " + cr_match.group(1)

    # Calendar type
    title_lower = event_title.lower()
    if "non-argument" in title_lower or "nac" in title_lower:
        cal_type = "Non-Argument Calendar"
    elif "motion" in title_lower:
        cal_type = "Motions"
    else:
        cal_type = "Argument Calendar"

    # Extract cases from tables
    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all(["td", "th"])
            cell_texts = [c.get_text(strip=True) for c in cells]

            if not cell_texts or len(cell_texts) < 2:
                continue

            docket_pattern = re.compile(r"\d{2,4}-\d{1,5}")
            docket_num = ""
            case_name = ""
            nature = ""
            description = ""
            case_link = ""

            for i, txt in enumerate(cell_texts):
                if docket_pattern.search(txt) and not docket_num:
                    docket_num = txt.strip()
                    links = cells[i].find_all("a", href=True)
                    if links:
                        case_link = links[0]["href"]
                        if case_link.startswith("/"):
                            case_link = BASE_URL + case_link
                elif txt and not docket_num:
                    continue
                elif txt and docket_num and not case_name:
                    case_name = txt.strip()
                elif txt and docket_num and case_name:
                    if not nature:
                        nature = txt.strip()
                    elif not description:
                        description = txt.strip()

            if docket_num:
                cases.append({
                    "Court Name": "2nd Circuit",
                    "Date": event_date,
                    "Time": event_time,
                    "Case Number": docket_num,
                    "Case Name": case_name,
                    "Judges / Panel": panel,
                    "Courtroom": courtroom,
                    "Location": "Thurgood Marshall U.S. Courthouse, New York, NY",
                    "Case Link": case_link,
                    "Nature of Case": nature,
                    "Description": f"{cal_type}. {description}".strip(". ") if description else cal_type,
                })

    # Fallback: parse from plain text
    if not cases:
        lines = page_text.split("\n")
        docket_pattern = re.compile(r"(\d{2,4}-\d{1,5})")
        for line in lines:
            m = docket_pattern.search(line)
            if m:
                docket_num = m.group(1)
                remainder = line[m.end():].strip(" :-–—")
                case_name = remainder if remainder else ""
                cases.append({
                    "Court Name": "2nd Circuit",
                    "Date": event_date,
                    "Time": event_time,
                    "Case Number": docket_num,
                    "Case Name": case_name,
                    "Judges / Panel": panel,
                    "Courtroom": courtroom,
                    "Location": "Thurgood Marshall U.S. Courthouse, New York, NY",
                    "Case Link": "",
                    "Nature of Case": "",
                    "Description": cal_type,
                })

    logger.info(f"Circuit 2: Parsed {len(cases)} cases from '{event_title}'")
    return cases


# ---------------------------------------------------------------------------
# Main scraper class
# ---------------------------------------------------------------------------

class USCA2Scraper:
    """Scraper for the Second Circuit oral argument calendar."""

    def __init__(self, verify_ssl: bool = True):
        self.verify_ssl = verify_ssl

    def scrape(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> pd.DataFrame:
        """
        Scrape Circuit 2 calendar and return a DataFrame with 11 columns.
        """
        if start_date is None:
            start_date = date.today()
        if end_date is None:
            end_date = date(start_date.year, start_date.month + 1, 1) if start_date.month < 12 \
                else date(start_date.year + 1, 1, 1)

        all_cases = []

        current = start_date.replace(day=1)
        while current <= end_date:
            year, month = current.year, current.month
            logger.info(f"Circuit 2: Scraping {year}-{month:02d}")

            # Stage 1: Get calendar month page
            url = f"{CALENDAR_URL}?yr={year}&m={month}&s=l&vm=r-h-f-l&bg=w"
            html = _fetch_page(url, wait_seconds=6, verify_ssl=self.verify_ssl)

            if not html:
                logger.warning(f"Circuit 2: Failed to fetch calendar for {year}-{month:02d}")
                current = _next_month(current)
                continue

            events = _parse_calendar_month(html, year, month)

            if not events:
                logger.info(f"Circuit 2: No events for {year}-{month:02d}")
                current = _next_month(current)
                continue

            # Stage 2: Get each event detail page
            # Reuse one driver for all detail pages in this month
            driver = None
            try:
                driver = _get_driver(verify_ssl=self.verify_ssl)
                for event in events:
                    try:
                        driver.get(event["event_url"])
                        time.sleep(3)
                        detail_html = driver.page_source
                        cases = _parse_event_detail(detail_html, event["event_title"])
                        all_cases.extend(cases)
                    except Exception as e:
                        logger.error(f"Circuit 2: Error on event '{event['event_title']}': {e}")
            finally:
                if driver:
                    try:
                        driver.quit()
                    except Exception:
                        pass

            current = _next_month(current)

        if not all_cases:
            return pd.DataFrame(columns=STANDARD_COLUMNS)

        df = pd.DataFrame(all_cases, columns=STANDARD_COLUMNS)

        # Filter to date range
        if "Date" in df.columns and not df.empty:
            df["_date"] = pd.to_datetime(df["Date"], errors="coerce")
            mask = (df["_date"] >= pd.Timestamp(start_date)) & (
                df["_date"] <= pd.Timestamp(end_date)
            )
            df = df.loc[mask].drop(columns=["_date"]).reset_index(drop=True)

        return df


def _next_month(d: date) -> date:
    """Return the first day of the next month."""
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)