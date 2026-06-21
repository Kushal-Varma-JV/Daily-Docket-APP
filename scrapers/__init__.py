"""
Scrapers package — exposes all circuit scrapers for easy import.
"""

from scrapers.circuit_1 import fetch_pdf_bytes, extract_text_from_pdf_bytes, parse_calendar_data
from scrapers.circuit_2 import USCA2Scraper
from scrapers.circuit_3 import CA3CourtScraper, normalize_c3_cases, run_c3_scraper_in_thread
from scrapers.circuit_4 import USCA4Scraper
from scrapers.circuit_5 import USCA5Scraper
from scrapers.circuit_6 import USCA6Scraper
from scrapers.circuit_7 import USCA7Scraper
from scrapers.circuit_8 import USCA8Scraper
from scrapers.circuit_9 import USCA9Scraper