"""
Fourth Circuit Court of Appeals — Oral Argument Calendar Scraper.
Parses PDF calendars from ca4.uscourts.gov.
"""

import re
import io
import time
from datetime import datetime
from typing import List, Dict
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from scrapers.base import _make_ssl_session
from utils.constants import DEFAULT_C4_BASE_URL, DEFAULT_C4_CALENDAR_URL
from utils.helpers import HAS_PDFPLUMBER, logger

if HAS_PDFPLUMBER:
    import pdfplumber


class USCA4Scraper:
    BASE_URL = DEFAULT_C4_BASE_URL
    CALENDAR_URL = DEFAULT_C4_CALENDAR_URL

    CATEGORIES = [
        'CONSTITUTIONAL LAW', 'CIVIL RIGHTS', 'CIVIL COMMITMENT',
        'CRIMINAL LAW', 'ELECTION LAW', 'FIRST AMENDMENT',
        'SECOND AMENDMENT', 'FOURTH AMENDMENT', 'HABEAS CORPUS',
        'INTELLECTUAL PROPERTY', 'NATIONAL LABOR RELATIONS',
        'BLACK LUNG', 'SOCIAL SECURITY', 'INITIAL HEARING EN BANC',
        'CIVIL', 'CRIMINAL', 'IMMIGRATION', 'SENTENCING', 'AGENCY',
        'BANKRUPTCY', 'EMPLOYMENT', 'TAX', 'PRISONER', 'ADMIRALTY',
        'ERISA', 'ENVIRONMENTAL', 'SECURITIES', 'ANTITRUST',
        'JURISDICTION', 'ARBITRATION', 'POSTCONVICTION', 'MANDAMUS',
        'INSURANCE', 'CONTRACT', 'TRADEMARK', 'LABOR',
    ]

    CATEGORY_PATTERN = re.compile(
        r'^(' + '|'.join(re.escape(c) for c in CATEGORIES) + r'):\s*(.*)',
        re.DOTALL | re.IGNORECASE,
    )
    PANEL_PATTERN = re.compile(r'PANEL\s+([IVXLCDM]+)', re.IGNORECASE)
    COURTROOM_PATTERN = re.compile(
        r'((?:Red|Blue|Gold|Tan|Butzner|Tweed|Green|Purple|Orange)\s+'
        r'(?:En Banc\s+)?Courtroom(?:\s*\(Room\s*[\w-]+\))?)',
        re.IGNORECASE,
    )
    TIME_PATTERN = re.compile(r'(\d{1,2}:\d{2}\s*[ap]\.m\.)', re.IGNORECASE)
    DATE_PATTERN = re.compile(
        r'((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+'
        r'(?:January|February|March|April|May|June|July|August|September|'
        r'October|November|December)\s+\d{1,2},\s+\d{4})',
        re.IGNORECASE,
    )
    CASE_NUM_PATTERN = re.compile(r'^(\d{2}-\d{4,5})\*?\s')

    STANDARD_COLUMNS = [
        'Case Name',
        'Case Number',
        'Nature of Case',
        'Court Name and Location',
        'Judge Name, Panel',
        'Courtroom Number',
        'Purpose of Hearing',
        'Description',
        'Date',
    ]

    def __init__(self, verify_ssl: bool = False):
        self.verify_ssl = verify_ssl
        self.session = _make_ssl_session()

    # ------------------------------------------------------------------
    # Calendar index
    # ------------------------------------------------------------------

    def fetch_calendar_index(self) -> List[Dict]:
        resp = self.session.get(self.CALENDAR_URL, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        entries = []
        current_section = None
        current_term = None

        for tag in soup.find_all(['h3', 'ul']):
            text = tag.get_text(strip=True)
            if tag.name == 'h3':
                lower = text.lower()
                if 'oral argument calendar' in lower and 'law school' not in lower:
                    current_section = 'regular'
                    current_term = None
                elif 'law school' in lower or 'special session' in lower:
                    current_section = 'law_school_special'
                    current_term = None
                elif 'term' in lower:
                    current_term = text
                continue

            if tag.name == 'ul' and current_section and current_term:
                for li in tag.find_all('li', recursive=False):
                    a_tag = li.find('a', href=True)
                    if a_tag and a_tag['href'].lower().endswith('.pdf'):
                        pdf_url = urljoin(self.BASE_URL, a_tag['href'])
                        label = li.get_text(strip=True)
                        entries.append({
                            'session_type': current_section,
                            'label': label,
                            'term': current_term,
                            'pdf_url': pdf_url,
                        })
                    elif li.get_text(strip=True):
                        entries.append({
                            'session_type': current_section,
                            'label': li.get_text(strip=True),
                            'term': current_term,
                            'pdf_url': None,
                        })
                current_section = None
                current_term = None

        if not entries:
            for a in soup.find_all('a', href=re.compile(r'/cal/.*\.pdf', re.I)):
                pdf_url = urljoin(self.BASE_URL, a['href'])
                label = a.get_text(strip=True)
                session_type = (
                    'law_school_special'
                    if re.search(r'(UofR|UVA|WVU|HighPoint|LawSchool|Special)', pdf_url, re.I)
                    else 'regular'
                )
                entries.append({
                    'session_type': session_type,
                    'label': label,
                    'term': 'Unknown',
                    'pdf_url': pdf_url,
                })
        return entries

    # ------------------------------------------------------------------
    # PDF word / line helpers
    # ------------------------------------------------------------------

    def _find_column_split(self, words):
        category_starts = []
        for w in words:
            for cat in self.CATEGORIES:
                first_word = cat.split()[0]
                if w['text'].upper().startswith(first_word) and w['x0'] > 200:
                    category_starts.append(w['x0'])
                    break
        if category_starts:
            return min(category_starts) - 5
        briefs_ends = [w['x1'] for w in words if w['text'] == 'Briefs']
        if briefs_ends:
            return max(briefs_ends) + 20
        return 370.0

    def _words_to_lines(self, words, y_tolerance=3):
        if not words:
            return []
        sorted_words = sorted(words, key=lambda w: (w['top'], w['x0']))
        lines = []
        current_line = {'top': sorted_words[0]['top'], 'words': [sorted_words[0]]}
        for w in sorted_words[1:]:
            if abs(w['top'] - current_line['top']) <= y_tolerance:
                current_line['words'].append(w)
            else:
                lines.append(current_line)
                current_line = {'top': w['top'], 'words': [w]}
        lines.append(current_line)
        for line in lines:
            line['words'].sort(key=lambda w: w['x0'])
        return lines

    def _reconstruct_text(self, words):
        if not words:
            return ""
        result = words[0]['text']
        for i in range(1, len(words)):
            gap = words[i]['x0'] - words[i - 1]['x1']
            result += (" " if gap > 4 else "") + words[i]['text']
        return result

    # ------------------------------------------------------------------
    # Extract panel info embedded in issue text
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_panel_info_from_text(text):
        """
        If panel info (PANEL X, judges, courtroom, time) got absorbed
        into the issue text, extract it out.
        Returns (clean_text, panel_info_dict) or (text, None).
        """
        panel_match = re.search(r'PANEL\s+([IVXLCDM]+)', text, re.IGNORECASE)
        if not panel_match:
            return text, None

        split_pos = panel_match.start()
        clean_text = text[:split_pos].strip()
        panel_block = text[split_pos:]

        info = {
            'panel_number': panel_match.group(1),
            'courtroom': None,
            'argument_time': None,
            'judges': None,
            'argument_date': None,
        }

        cr_match = re.search(
            r'((?:Red|Blue|Gold|Tan|Butzner|Tweed|Green|Purple|Orange)\s+'
            r'(?:En Banc\s+)?Courtroom(?:\s*\(Room\s*[\w-]+\))?)',
            panel_block, re.IGNORECASE,
        )
        if cr_match:
            info['courtroom'] = cr_match.group(1).strip()

        time_match = re.search(r'(\d{1,2}:\d{2}\s*[ap]\.m\.)', panel_block, re.IGNORECASE)
        if time_match:
            info['argument_time'] = time_match.group(1).strip()

        date_match = re.search(
            r'((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+'
            r'(?:January|February|March|April|May|June|July|August|September|'
            r'October|November|December)\s+\d{1,2},\s+\d{4})',
            panel_block, re.IGNORECASE,
        )
        if date_match:
            info['argument_date'] = date_match.group(1).strip()

        judges_text = panel_block[panel_match.end():]
        cut_points = []
        if cr_match:
            cut_points.append(cr_match.start() - panel_match.end())
        if time_match:
            cut_points.append(time_match.start() - panel_match.end())
        if cut_points:
            judges_text = judges_text[:min(cut_points)]
        judges_text = re.sub(r'\s+', ' ', judges_text).strip(' ,;')
        if judges_text:
            info['judges'] = judges_text

        return clean_text, info

    # ------------------------------------------------------------------
    # Location / date cleaners
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_location(raw_location):
        """Strip REVD notes and junk, extract just the city."""
        if not raw_location:
            return ''
        # Try to extract "City, ST" pattern
        city_match = re.search(
            r'(Richmond|Charlotte|Baltimore|Raleigh|Charleston|Lexington'
            r'|Charlottesville|Washington),?\s*'
            r'(VA|NC|MD|SC|WV|DC)?',
            raw_location, re.IGNORECASE,
        )
        if city_match:
            city = city_match.group(1).strip()
            state = city_match.group(2)
            if state:
                return f"{city}, {state.upper()}"
            return city
        # Fallback: remove REVD junk and return what's left
        cleaned = re.sub(r'REVD\s*\d{1,2}/\d{1,2}/\d{4}', '', raw_location)
        cleaned = re.sub(r'\s*\|\s*', ' ', cleaned)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip(' |')
        return cleaned

    @staticmethod
    def _format_date(raw_date):
        """Convert 'Tuesday, September 9, 2025' → '2025-09-09'."""
        if not raw_date:
            return ''
        cleaned = re.sub(
            r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*',
            '', raw_date, flags=re.IGNORECASE,
        )
        for fmt in ('%B %d, %Y', '%B %d %Y', '%b %d, %Y', '%b %d %Y'):
            try:
                return datetime.strptime(cleaned.strip(), fmt).strftime('%Y-%m-%d')
            except ValueError:
                continue
        return raw_date

    # ------------------------------------------------------------------
    # PDF parser
    # ------------------------------------------------------------------

    def _parse_pdf_pdfplumber(self, pdf_bytes, pdf_url):
        all_cases = []
        pending_cases = []
        location = None
        current_page_date = None

        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page_num, page in enumerate(pdf.pages):
                words = page.extract_words(keep_blank_chars=True, x_tolerance=3, y_tolerance=3)
                if not words:
                    continue
                col_split = self._find_column_split(words)
                lines = self._words_to_lines(words)

                if page_num == 0 and not location:
                    header_lines = []
                    for line in lines:
                        text = self._reconstruct_text(line['words'])
                        if 'UNITED STATES COURT OF APPEALS' in text:
                            continue
                        if text.startswith('Page '):
                            continue
                        if '___' in text:
                            break
                        header_lines.append(text.strip())
                    if header_lines:
                        location = ' | '.join(l for l in header_lines if l)

                for line in lines:
                    text = self._reconstruct_text(line['words'])
                    dm = self.DATE_PATTERN.match(text.strip())
                    if dm and line['top'] < 120:
                        current_page_date = text.strip()

                separator_tops = []
                for line in lines:
                    text = self._reconstruct_text(line['words'])
                    if re.match(r'^_{10,}$', text.replace(' ', '')):
                        separator_tops.append(line['top'])

                for i, sep_top in enumerate(separator_tops):
                    block_top = sep_top
                    block_bottom = separator_tops[i + 1] if i + 1 < len(separator_tops) else page.height
                    block_lines = [l for l in lines if l['top'] > block_top and l['top'] < block_bottom]
                    if not block_lines:
                        continue

                    left_line_texts = []
                    right_line_texts = []
                    for line in block_lines:
                        left_words = [w for w in line['words'] if w['x0'] < col_split]
                        right_words = [w for w in line['words'] if w['x0'] >= col_split]
                        if left_words:
                            left_line_texts.append(self._reconstruct_text(left_words))
                        if right_words:
                            right_line_texts.append(self._reconstruct_text(right_words))

                    left_text = '\n'.join(left_line_texts)
                    right_text = ' '.join(right_line_texts)

                    case_num_match = self.CASE_NUM_PATTERN.match(left_text)
                    if case_num_match:
                        case_number = case_num_match.group(1)
                        left_remaining = left_text[case_num_match.end():].strip()
                        left_remaining = re.sub(r'^Briefs\s*', '', left_remaining, count=1).strip()

                        associations = None
                        assoc_match = re.search(r'Associations?:\s*([\d\-,\s]+)', left_remaining)
                        if assoc_match:
                            associations = re.sub(r'\s+', ' ', assoc_match.group(1)).strip()
                            left_remaining = left_remaining[:assoc_match.start()].strip()

                        lower_court_judge = None
                        judge_match = re.search(r'\(([^)]+)\)\s*$', left_remaining)
                        if judge_match:
                            lower_court_judge = judge_match.group(1)
                            left_remaining = left_remaining[:judge_match.start()].strip()

                        case_name = re.sub(r'\s+', ' ', left_remaining).strip()

                        category = None
                        issue = right_text.strip()
                        cat_match = self.CATEGORY_PATTERN.match(issue)
                        if cat_match:
                            category = cat_match.group(1).upper()
                            issue = cat_match.group(2).strip()

                        # Check if panel info is embedded in issue text
                        clean_issue, embedded_panel = self._extract_panel_info_from_text(issue)
                        if embedded_panel:
                            issue = clean_issue

                        case_dict = {
                            'case_number': case_number,
                            'case_name': case_name,
                            'lower_court_judge': lower_court_judge,
                            'category': category,
                            'issue_description': re.sub(r'\s+', ' ', issue).strip(),
                            'associations': associations,
                            'panel_number': embedded_panel['panel_number'] if embedded_panel else None,
                            'courtroom': embedded_panel['courtroom'] if embedded_panel else None,
                            'argument_time': embedded_panel['argument_time'] if embedded_panel else None,
                            'judges': embedded_panel['judges'] if embedded_panel else None,
                            'argument_date': embedded_panel['argument_date'] if embedded_panel else current_page_date,
                            'location': location,
                            'pdf_url': pdf_url,
                        }
                        pending_cases.append(case_dict)
                        continue

                    combined = left_text + ' ' + right_text
                    panel_match = self.PANEL_PATTERN.search(combined)
                    time_match = self.TIME_PATTERN.search(combined)
                    date_match = self.DATE_PATTERN.search(combined)
                    courtroom_match = self.COURTROOM_PATTERN.search(combined)
                    is_panel_block = (
                        panel_match
                        or (time_match and date_match)
                        or (time_match and 'LIVESTREAM' in combined.upper())
                    )

                    if is_panel_block:
                        judges_str = None
                        if panel_match:
                            after_panel = combined[panel_match.end():]
                            cut_points = []
                            if courtroom_match:
                                cr_pos = combined.index(courtroom_match.group(0))
                                if cr_pos > panel_match.end():
                                    cut_points.append(cr_pos - panel_match.end())
                            if time_match:
                                tm_pos = combined.index(time_match.group(1))
                                if tm_pos > panel_match.end():
                                    cut_points.append(tm_pos - panel_match.end())
                            if cut_points:
                                judges_str = after_panel[:min(cut_points)]
                            else:
                                judges_str = after_panel
                            judges_str = re.sub(r'\s+', ' ', judges_str).strip(' ,;')
                            if not judges_str:
                                judges_str = None

                        for case_dict in pending_cases:
                            if not case_dict.get('panel_number'):
                                case_dict['panel_number'] = panel_match.group(1) if panel_match else None
                            if not case_dict.get('courtroom'):
                                case_dict['courtroom'] = courtroom_match.group(0).strip() if courtroom_match else None
                            if not case_dict.get('argument_time'):
                                case_dict['argument_time'] = time_match.group(1) if time_match else None
                            if not case_dict.get('judges'):
                                case_dict['judges'] = judges_str
                            if date_match and not case_dict.get('argument_date'):
                                case_dict['argument_date'] = date_match.group(0)
                        all_cases.extend(pending_cases)
                        pending_cases = []

        if pending_cases:
            all_cases.extend(pending_cases)
        return all_cases

    def parse_calendar_pdf(self, pdf_url):
        resp = self.session.get(pdf_url, timeout=60)
        resp.raise_for_status()
        if HAS_PDFPLUMBER:
            return self._parse_pdf_pdfplumber(resp.content, pdf_url)
        return []

    # ------------------------------------------------------------------
    # Normalize to standard 9-field schema
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_cases(raw_cases, session_type, session_label, term):
        normalized = []
        for c in raw_cases:
            # --- Nature of Case: just the category ---
            nature = c.get('category', '') or ''

            # --- Court Name and Location ---
            raw_loc = c.get('location', '') or ''
            city = USCA4Scraper._clean_location(raw_loc)
            if city:
                court_loc = f"4th Circuit — {city}"
            else:
                court_loc = '4th Circuit'

            # --- Judge Name, Panel ---
            judges = c.get('judges', '') or ''
            panel_num = c.get('panel_number', '') or ''
            if judges and panel_num:
                judge_panel = f"Panel {panel_num} — {judges}"
            elif judges:
                judge_panel = judges
            elif panel_num:
                judge_panel = f"Panel {panel_num}"
            else:
                judge_panel = ''

            # --- Courtroom Number ---
            courtroom = c.get('courtroom', '') or ''

            # --- Description: the issue text ---
            description = c.get('issue_description', '') or ''

            # --- Date ---
            raw_date = c.get('argument_date', '') or ''
            formatted_date = USCA4Scraper._format_date(raw_date)

            normalized.append({
                'Case Name': c.get('case_name', '') or '',
                'Case Number': c.get('case_number', '') or '',
                'Nature of Case': nature,
                'Court Name and Location': court_loc,
                'Judge Name, Panel': judge_panel,
                'Courtroom Number': courtroom,
                'Purpose of Hearing': 'Oral Argument',
                'Description': description,
                'Date': formatted_date,
            })
        return normalized

    def scrape_all(self, progress_callback=None):
        entries = self.fetch_calendar_index()
        all_cases = []
        pdf_entries = [e for e in entries if e['pdf_url']]
        total = len(pdf_entries)

        for idx, entry in enumerate(pdf_entries):
            if progress_callback:
                progress_callback(idx + 1, total, entry['label'])
            try:
                raw_cases = self.parse_calendar_pdf(entry['pdf_url'])
                normalized = self.normalize_cases(
                    raw_cases, entry['session_type'], entry['label'], entry['term']
                )
                all_cases.extend(normalized)
            except Exception as e:
                logger.error(f"  Failed to parse {entry['pdf_url']}: {e}")
            time.sleep(0.5)
        return all_cases