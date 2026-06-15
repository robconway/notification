#!/usr/bin/env python3
"""
North Devon Planning Notifications

Strategy 1: Playwright fetch of the weekly viewer (my.northdevon.gov.uk).
Strategy 2: GovDelivery bulletin – if it just links to the viewer, follow
            that link with Playwright.
Strategy 3: Direct date-range search of planning.northdevon.gov.uk.

Applications within RADIUS_MILES of home (EX33 2LD, Braunton) trigger
an email.  Seen references are stored in state.json (committed each run).

Run manually:
    python notify.py
    python notify.py --dry-run        # print matches, skip email/state
    python notify.py --dump-html      # save raw HTML for debugging
    python notify.py --strategy 3     # force direct portal search
"""

import argparse
import json
import os
import re
import smtplib
import sys
import time
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from math import atan2, cos, radians, sin, sqrt
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HOME_LAT = 51.10264     # EX33 2LD – Braunton, North Devon
HOME_LON = -4.16247
HOME_POSTCODE = "EX33 2LD"
RADIUS_MILES = float(os.getenv("RADIUS_MILES", "1"))

VIEWER_URL = (
    "https://my.northdevon.gov.uk/service/Weekly_planning_report_viewer"
    "?fromemail=newly%20registered"
)

PLANNING_DISPLAY_URL = "https://planning.northdevon.gov.uk/Planning/Display/{ref}"
PLANNING_SEARCH_URL  = "https://planning.northdevon.gov.uk/Planning/Search"

STATE_FILE   = "state.json"
POSTCODES_IO = "https://api.postcodes.io/postcodes/{}"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d{1,2}[A-Z]?\s*\d[A-Z]{2})\b", re.IGNORECASE)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-GB,en;q=0.9",
}

# ---------------------------------------------------------------------------
# Distance
# ---------------------------------------------------------------------------


def haversine_miles(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 3958.8 * 2 * atan2(sqrt(a), sqrt(1 - a))


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------


def geocode_postcode(postcode):
    clean = postcode.strip().upper().replace(" ", "")
    try:
        r = requests.get(POSTCODES_IO.format(clean), timeout=10)
        if r.ok:
            d = r.json().get("result", {})
            return d.get("latitude"), d.get("longitude")
    except Exception:
        pass
    return None, None


def geocode_nominatim(address):
    time.sleep(1)
    params = {"q": f"{address}, North Devon, UK", "format": "json",
              "limit": 1, "countrycodes": "gb"}
    try:
        r = requests.get(
            NOMINATIM_URL, params=params,
            headers={"User-Agent": "NorthDevonPlanningNotifier/1.0 rob@medberry.co.uk"},
            timeout=15,
        )
        results = r.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception:
        pass
    return None, None


def geocode_address(address):
    m = POSTCODE_RE.search(address)
    if m:
        lat, lon = geocode_postcode(m.group(0))
        if lat is not None:
            return lat, lon
    return geocode_nominatim(address)


# ---------------------------------------------------------------------------
# HTML parsing (shared)
# ---------------------------------------------------------------------------

_DISPLAY_RE = re.compile(r"/[Dd]isplay/(\d{4,6})", re.IGNORECASE)
_REF_RE     = re.compile(r"\b[789]\d{4}\b")   # 5-digit refs starting 7/8/9


def _ref_from_text(text):
    m = _REF_RE.search(text)
    return m.group(0) if m else None


def parse_applications_from_html(html, source_label=""):
    """
    Extract planning applications from HTML using four patterns (tried in order):
    0. /Planning/Display/<N> in href attributes
    1. Table rows
    2. List items / divs / paragraphs containing a reference
    3. Anchor link text matching a reference
    """
    soup = BeautifulSoup(html, "lxml")
    apps = []
    seen = set()

    # Pattern 0: hrefs containing /Display/<ref>
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = _DISPLAY_RE.search(href)
        if not m:
            continue
        ref = m.group(1)
        if ref in seen:
            continue
        seen.add(ref)
        parent = a.parent
        surrounding = parent.get_text(" ", strip=True)[:300] if parent else a.get_text(strip=True)
        url = href if href.startswith("http") else f"https://planning.northdevon.gov.uk{href}"
        apps.append({"reference": ref, "address": surrounding, "description": "", "url": url, "source": source_label})

    # Pattern 1: table rows
    for table in soup.find_all("table"):
        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            texts = [c.get_text(strip=True) for c in cells]
            ref = _ref_from_text(texts[0]) or _ref_from_text(" ".join(texts))
            if not ref or ref in seen:
                continue
            seen.add(ref)
            link = cells[0].find("a")
            url = link["href"] if link else PLANNING_DISPLAY_URL.format(ref=ref)
            if url.startswith("/"):
                url = "https://planning.northdevon.gov.uk" + url
            apps.append({
                "reference": ref,
                "address": texts[1] if len(texts) > 1 else "",
                "description": texts[2] if len(texts) > 2 else "",
                "url": url,
                "source": source_label,
            })

    # Pattern 2: list items / divs / paragraphs
    if not apps:
        for item in soup.find_all(["li", "div", "p"]):
            text = item.get_text(" ", strip=True)
            ref = _ref_from_text(text)
            if not ref or ref in seen:
                continue
            seen.add(ref)
            link = item.find("a")
            url = ""
            if link:
                url = link.get("href", "")
                if url.startswith("/"):
                    url = "https://planning.northdevon.gov.uk" + url
            apps.append({
                "reference": ref,
                "address": text[:200],
                "description": "",
                "url": url or PLANNING_DISPLAY_URL.format(ref=ref),
                "source": source_label,
            })

    # Pattern 3: anchor text matching a reference
    if not apps:
        for a in soup.find_all("a", href=True):
            ref = _ref_from_text(a.get_text(strip=True))
            if ref and ref not in seen:
                seen.add(ref)
                parent_text = a.parent.get_text(" ", strip=True)[:200] if a.parent else ""
                url = a["href"]
                if url.startswith("/"):
                    url = "https://planning.northdevon.gov.uk" + url
                apps.append({
                    "reference": ref, "address": parent_text,
                    "description": "", "url": url, "source": source_label,
                })

    return apps


def _debug_snippet(label, html):
    """Print first 3000 chars of HTML to stdout for GitHub Actions log visibility."""
    snippet = html.replace("\n", " ").replace("\r", "")[:3000]
    print(f"\n[DEBUG] {label} – first 3000 chars of HTML:\n{snippet}\n")


def parse_viewer_text_applications(viewer_text, viewer_html):
    """
    Parse planning applications from the North Devon viewer iframe inner text.

    The viewer produces structured text blocks separated by "Application Number:":
      Application Number: 81832
      Site Address:

      Strand House, Braunton Road, Barnstaple, Devon, EX31 4AU

      Description:

      Listed Building Consent to regularise...

    We pair those text blocks with the display URLs found in the iframe HTML.
    """
    # Collect /Planning/Display/<ref> URLs from the iframe HTML
    soup = BeautifulSoup(viewer_html, "lxml")
    app_urls = {}
    for a in soup.find_all("a", href=True):
        m = _DISPLAY_RE.search(a["href"])
        if m:
            ref = m.group(1)
            if ref not in app_urls:
                href = a["href"]
                app_urls[ref] = (
                    href if href.startswith("http")
                    else "https://planning.northdevon.gov.uk" + href
                )

    apps = []
    seen = set()
    for part in re.split(r"Application Number:\s*", viewer_text)[1:]:
        ref_m = re.match(r"(\d{4,6})", part.strip())
        if not ref_m:
            continue
        ref = ref_m.group(1)
        if ref in seen:
            continue
        seen.add(ref)

        # Site address: text after "Site Address:" up to the next blank line
        addr_m = re.search(r"Site Address:[^\n]*\n+(.*?)\n\n", part, re.DOTALL)
        address = " ".join(addr_m.group(1).split()) if addr_m else ""

        # Description: text after "Description:" up to the next blank line
        desc_m = re.search(r"Description:[^\n]*\n+(.*?)\n\n", part, re.DOTALL)
        desc = " ".join(desc_m.group(1).split()) if desc_m else ""

        url = app_urls.get(ref, PLANNING_DISPLAY_URL.format(ref=ref))
        apps.append({
            "reference": ref,
            "address": address,
            "description": desc,
            "url": url,
            "source": "viewer",
        })
    return apps


# ---------------------------------------------------------------------------
# Playwright helper
# ---------------------------------------------------------------------------


def _playwright_fetch(url, label, login_url=None, dump_html=False, dump_name=None):
    """
    Load *url* with a headless Chromium browser and return the page HTML.
    If the browser lands on a login page, optionally attempt credentials.
    Returns empty string on failure.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(f"[{label}] playwright not installed – skipping", file=sys.stderr)
        return ""

    html = ""
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                ),
                locale="en-GB",
            )
            page = ctx.new_page()
            page.goto(url, wait_until="networkidle", timeout=45_000)
            print(f"[{label}] landed on: {page.url}")

            # Detect a login form (password input anywhere on page)
            has_password = page.query_selector('input[type="password"]') is not None
            if has_password:
                username = os.getenv("PORTAL_USERNAME", "")
                password = os.getenv("PORTAL_PASSWORD", "")
                if username and password:
                    print(f"[{label}] Login form detected – attempting login")
                    try:
                        page.fill('input[type="email"], input[name*="user"], input[name*="email"], input[type="text"]', username)
                        page.fill('input[type="password"]', password)
                        page.click('button[type="submit"], input[type="submit"]')
                        page.wait_for_load_state("networkidle", timeout=20_000)
                        print(f"[{label}] After login, landed on: {page.url}")
                        if login_url and page.url != url:
                            page.goto(url, wait_until="networkidle", timeout=30_000)
                    except Exception as e:
                        print(f"[{label}] Login attempt failed: {e}", file=sys.stderr)
                else:
                    print(f"[{label}] Login form found but PORTAL_USERNAME/PASSWORD not set")

            html = page.content()
            browser.close()
    except Exception as e:
        print(f"[{label}] Playwright error: {e}", file=sys.stderr)
        return ""

    if dump_html and dump_name:
        with open(dump_name, "w") as fh:
            fh.write(html)
        print(f"[{label}] HTML saved to {dump_name}")

    return html


# ---------------------------------------------------------------------------
# Strategy 1: weekly viewer via Playwright (with interactive clicks)
# ---------------------------------------------------------------------------


def _dump_page_diagnostics(page_or_frame, label):
    """Print page/frame text + links + buttons to log for debugging."""
    try:
        body_text = page_or_frame.inner_text("body")
        print(f"[{label}] page text (first 800): {body_text[:800]!r}")
        all_links = page_or_frame.query_selector_all("a[href]")
        link_sample = [(a.inner_text().strip()[:60], (a.get_attribute("href") or "")[:80])
                       for a in all_links[:30]]
        print(f"[{label}] links ({len(all_links)} total): {link_sample}")
        all_buttons = page_or_frame.query_selector_all("button, [role=tab], [role=button], input[type=submit]")
        btn_sample = [b.inner_text().strip()[:60] for b in all_buttons[:20]]
        print(f"[{label}] buttons/tabs ({len(all_buttons)} total): {btn_sample}")
    except Exception as _e:
        print(f"[{label}] diagnostic error: {_e}")


def fetch_via_viewer(dump_html=False):
    """
    Load the viewer at my.northdevon.gov.uk:
      Step 0: Accept the Granicus cookie consent banner
      Step 1: Find the iframe that contains the form content (Granicus Self embeds
              the form in a child frame; page.inner_text() doesn't capture it)
      Step 2: Click 'Newly registered' tab inside the frame
      Step 3: Click the most recent date inside the frame
    """
    print("[strategy 1] Loading weekly viewer via Playwright …")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[strategy 1] playwright not installed")
        return []

    html = ""
    viewer_text = ""
    viewer_html_content = ""
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                ),
                locale="en-GB",
            )
            page = ctx.new_page()
            page.goto(VIEWER_URL, wait_until="networkidle", timeout=45_000)
            print(f"[strategy 1] landed on: {page.url}")

            # Step 0: dismiss cookie consent banner
            _cookie_accepted = False
            for cookie_sel in [
                'button:has-text("Accept all")',
                'button:has-text("Accept cookies")',
                'button:has-text("Accept")',
                'button:has-text("I agree")',
                'button:has-text("Agree")',
                'button:has-text("Got it")',
                'button:has-text("OK")',
                'a:has-text("Accept all")',
                'a:has-text("Accept cookies")',
                '[id*="cookie"] button',
                '[class*="cookie"] button',
            ]:
                try:
                    el = page.locator(cookie_sel).first
                    if el.count() > 0 and el.is_visible(timeout=1000):
                        el.click()
                        time.sleep(0.8)
                        print(f"[strategy 1] Accepted cookie consent via: {cookie_sel}")
                        _cookie_accepted = True
                        break
                except Exception:
                    continue

            if not _cookie_accepted:
                print("[strategy 1] No cookie consent found (or already accepted)")

            # Wait for content (the form may be in an iframe that loads after cookies)
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass
            time.sleep(2)

            # Step 1: find the iframe containing the planning viewer content.
            # Granicus Self embeds the actual form in a child frame; the outer
            # page only shows the navigation shell.
            all_frames = page.frames
            print(f"[strategy 1] {len(all_frames)} frame(s) detected")
            for i, fr in enumerate(all_frames):
                try:
                    fr_url = fr.url or "(blank)"
                    try:
                        fr_text = fr.inner_text("body")[:200]
                    except Exception:
                        fr_text = "(unreadable)"
                    print(f"[strategy 1] frame[{i}]: url={fr_url!r} text={fr_text!r}")
                except Exception as fe:
                    print(f"[strategy 1] frame[{i}] error: {fe}")

            # Identify which frame holds the viewer content
            viewer_frame = None
            for fr in all_frames:
                try:
                    text = fr.inner_text("body").lower()
                    if any(kw in text for kw in [
                        "planning", "registered", "application", "report", "section 1",
                        "newly", "recently", "week", "date",
                    ]):
                        print(f"[strategy 1] Planning content found in frame: {fr.url!r}")
                        viewer_frame = fr
                        break
                except Exception:
                    continue

            if viewer_frame is None:
                print("[strategy 1] No viewer frame found – using main page")

            target = viewer_frame or page

            # Take a screenshot when debugging to see the actual rendered state
            if dump_html:
                try:
                    page.screenshot(path="debug_strategy1_screenshot.png", full_page=True)
                    print("[strategy 1] Screenshot saved to debug_strategy1_screenshot.png")
                except Exception as se:
                    print(f"[strategy 1] Screenshot failed: {se}")

            _dump_page_diagnostics(target, "strategy 1 target")

            # Step 2: click the "Newly registered" tab
            _newly_clicked = False
            for sel in [
                'text="Newly registered"',
                'text="Recently registered"',
                'text="newly registered"',
                'text="recently registered"',
                '[data-tab*="register" i]',
                'a[href*="newly"]',
                'a[href*="registered" i]',
                'button:has-text("Newly")',
                'button:has-text("registered")',
                'li.active:has-text("register")',
            ]:
                try:
                    el = target.locator(sel).first
                    if el.count() > 0 and el.is_visible(timeout=1000):
                        el.click()
                        try:
                            page.wait_for_load_state("networkidle", timeout=10_000)
                        except Exception:
                            pass
                        print(f"[strategy 1] Clicked 'Newly registered' via: {sel}")
                        _newly_clicked = True
                        break
                except Exception:
                    continue

            if not _newly_clicked:
                print("[strategy 1] 'Newly registered' tab not found (URL param may have pre-selected it)")

            # Step 3: click the first (most recent) date
            _date_clicked = False
            for sel in [
                'a[href*="date="]',
                'a[href*="week="]',
                'a[href*="report"]',
                '.date-list a',
                '.week-list a',
                'table.calendar td a',
                'a:has-text("2026")',
                'a:has-text("2025")',
            ]:
                try:
                    items = target.locator(sel)
                    if items.count() > 0:
                        items.first.click()
                        try:
                            page.wait_for_load_state("networkidle", timeout=10_000)
                        except Exception:
                            pass
                        print(f"[strategy 1] Clicked first date via: {sel}")
                        _date_clicked = True
                        break
                except Exception:
                    continue

            if not _date_clicked:
                print("[strategy 1] Could not find a date to click")

            if _date_clicked or _newly_clicked:
                _dump_page_diagnostics(target, "strategy 1 post-click")

            # Capture viewer frame text + HTML for the accurate text parser
            if viewer_frame:
                try:
                    viewer_text = viewer_frame.inner_text("body")
                    viewer_html_content = viewer_frame.content()
                except Exception as ve:
                    print(f"[strategy 1] Could not read viewer frame content: {ve}")

            # Concatenate HTML from all frames for fallback HTML parser
            html_parts = []
            for fr in all_frames:
                try:
                    html_parts.append(fr.content())
                except Exception:
                    pass
            html = "\n".join(html_parts) if html_parts else page.content()

            browser.close()
    except Exception as e:
        print(f"[strategy 1] Playwright error: {e}")
        return []

    if dump_html:
        with open("debug_strategy1.html", "w") as fh:
            fh.write(html)
        print("[strategy 1] HTML saved to debug_strategy1.html")

    # Primary: text parser extracts accurate site addresses from the structured
    # inner text the viewer iframe produces
    if viewer_text:
        apps = parse_viewer_text_applications(viewer_text, viewer_html_content)
        print(f"[strategy 1] Parsed {len(apps)} application(s) via text parser")
        if apps:
            return apps

    # Fallback: generic HTML parser
    apps = parse_applications_from_html(html, source_label="viewer")
    print(f"[strategy 1] Parsed {len(apps)} application(s) via HTML parser")
    if not apps:
        _debug_snippet("strategy 1 viewer", html)
    return apps


# ---------------------------------------------------------------------------
# Strategy 2: GovDelivery bulletin → follow viewer link
# ---------------------------------------------------------------------------

_KNOWN_BULLETIN_IDS = [
    "3daba2e",  # Recently registered planning applications Update (~Apr 2025)
    "3b48b71",  # 06 Sep 2024 recently registered
    "39efbdb",  # 24 May 2024 recently registered
    "3863cc4",  # 19 Jan 2024 recently registered
    "380872a",  # 15 Dec 2023
]

_GOVDELIVERY_BASE = "https://content.govdelivery.com/accounts/UKNORTHDEVON/bulletins/{}"


def _fetch_bulletin_html(hex_id):
    url = _GOVDELIVERY_BASE.format(hex_id)
    try:
        r = requests.get(url, headers=_HEADERS, timeout=20)
        if r.ok and "planning" in r.text.lower():
            return r.text
    except Exception:
        pass
    return None


def _find_latest_bulletin():
    """
    Return (hex_id, html) for the most recent 'recently/newly registered'
    bulletin.  Searches a narrow window of ±3 weeks around the estimated
    current bulletin ID, plus all known IDs as fallbacks.
    """
    WEEK_INCREMENT = 87_000

    start_int  = int(_KNOWN_BULLETIN_IDS[0], 16)
    start_date = datetime(2025, 4, 1)
    weeks_elapsed = max(0, int((datetime.now() - start_date).days / 7))
    estimate = start_int + weeks_elapsed * WEEK_INCREMENT

    # Narrow window: ±3 weeks around estimate + all known IDs
    candidates = [estimate + offset * WEEK_INCREMENT for offset in range(-3, 4)]
    for known in _KNOWN_BULLETIN_IDS:
        candidates.append(int(known, 16))
    candidates = sorted(set(candidates), reverse=True)

    for cand_int in candidates:
        hex_id = format(cand_int, "x")
        print(f"[strategy 2] Trying bulletin {hex_id} …")
        html = _fetch_bulletin_html(hex_id)
        if html:
            lower = html.lower()
            if any(p in lower for p in (
                "newly registered", "newly+registered",
                "recently registered", "recently+registered",
            )):
                print(f"[strategy 2] Found matching bulletin: {hex_id}")
                return hex_id, html
        time.sleep(0.3)

    return None, None


def _extract_northdevon_links(html):
    """Return all unique northdevon.gov.uk links from a bulletin."""
    soup = BeautifulSoup(html, "lxml")
    links = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "northdevon.gov.uk" in href and href not in seen:
            seen.add(href)
            links.append(href)
    return links


def fetch_via_govdelivery(dump_html=False):
    print("[strategy 2] Searching GovDelivery bulletins …")
    hex_id, bulletin_html = _find_latest_bulletin()

    if not bulletin_html:
        print("[strategy 2] Could not find a recent bulletin")
        return []

    if dump_html:
        fname = f"debug_bulletin_{hex_id}.html"
        with open(fname, "w") as fh:
            fh.write(bulletin_html)
        print(f"[strategy 2] Bulletin HTML saved to {fname}")

    # Try parsing applications directly from the bulletin
    apps = parse_applications_from_html(bulletin_html, source_label=f"bulletin/{hex_id}")
    print(f"[strategy 2] Parsed {len(apps)} application(s) directly from bulletin")

    if apps:
        return apps

    # Bulletin probably just links to the viewer – extract those links and
    # load each with Playwright
    nd_links = _extract_northdevon_links(bulletin_html)
    print(f"[strategy 2] Bulletin contains {len(nd_links)} northdevon.gov.uk link(s): {nd_links}")

    if not nd_links:
        _debug_snippet(f"bulletin/{hex_id}", bulletin_html)
        return []

    for link_url in nd_links:
        print(f"[strategy 2] Loading bulletin link: {link_url}")
        # If the link is the weekly viewer, use the interactive click sequence
        if "weekly_planning_report_viewer" in link_url.lower() or \
           "my.northdevon.gov.uk" in link_url.lower():
            print("[strategy 2] Recognised viewer link – using interactive click sequence")
            apps = fetch_via_viewer(dump_html=dump_html)
        else:
            html = _playwright_fetch(
                link_url,
                label="strategy 2 link",
                login_url=link_url,
                dump_html=dump_html,
                dump_name=f"debug_bulletin_link_{hex_id}.html",
            )
            if not html:
                continue
            apps = parse_applications_from_html(html, source_label=f"bulletin-link/{hex_id}")
            print(f"[strategy 2] Parsed {len(apps)} application(s) from link")
            if not apps:
                _debug_snippet(f"bulletin link {link_url}", html)
        if apps:
            return apps

    return []


# ---------------------------------------------------------------------------
# Strategy 3: direct search of planning.northdevon.gov.uk
# ---------------------------------------------------------------------------


def fetch_via_portal_search(days_back=7, dump_html=False):
    """
    Search the planning portal directly for applications registered in the
    last *days_back* days.  Uses a short connect timeout — if the host is
    unreachable (geo-blocked from GitHub Actions) this fails fast.
    """
    today    = date.today()
    from_dt  = today - timedelta(days=days_back)
    date_fmt = "%d/%m/%Y"

    params = {
        "searchType": "Application",
        "applicantname": "",
        "apptype": "",
        "sttype": "",
        "status": "REGAPL",
        "date1": from_dt.strftime(date_fmt),
        "date2": today.strftime(date_fmt),
        "ward": "",
        "parish": "",
        "district": "",
        "sorter": "",
        "submit.x": "31",
        "submit.y": "11",
    }

    search_url = f"{PLANNING_SEARCH_URL}?{urlencode(params)}"
    print(f"[strategy 3] Searching portal: {search_url}")

    try:
        # (connect_timeout=5s, read_timeout=20s) — fails fast if geo-blocked
        r = requests.get(PLANNING_SEARCH_URL, params=params, headers=_HEADERS, timeout=(5, 20))
        if r.ok and len(r.text) > 500:
            if dump_html:
                with open("debug_strategy3_requests.html", "w") as fh:
                    fh.write(r.text)
            apps = parse_applications_from_html(r.text, source_label="portal-search")
            print(f"[strategy 3] requests: parsed {len(apps)} application(s)")
            if apps:
                return apps
            _debug_snippet("strategy 3 requests", r.text)
        else:
            print(f"[strategy 3] HTTP {r.status_code} – skipping")
    except Exception as e:
        print(f"[strategy 3] portal unreachable (likely geo-blocked from CI): {e}")
        return []

    return []


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def load_seen():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as fh:
            return set(json.load(fh).get("seen", []))
    return set()


def save_seen(seen):
    with open(STATE_FILE, "w") as fh:
        json.dump(
            {
                "seen": sorted(seen),
                "updated": datetime.now(tz=__import__("datetime").timezone.utc).isoformat(),
            },
            fh,
            indent=2,
        )


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def send_email(applications):
    smtp_host     = os.environ["SMTP_HOST"]
    smtp_port     = int(os.getenv("SMTP_PORT", "587"))
    email_from    = os.environ["EMAIL_FROM"]
    email_password = os.environ["EMAIL_PASSWORD"]
    email_to      = os.getenv("EMAIL_TO", "rob@medberry.co.uk")

    count   = len(applications)
    subject = (
        f"Planning alert: {count} new application{'s' if count != 1 else ''} "
        f"within {RADIUS_MILES:.0f} mile of {HOME_POSTCODE}"
    )

    text_lines = [subject, "=" * len(subject), ""]
    html_rows  = []

    for app in applications:
        dist     = app.get("distance_miles")
        dist_str = f"{dist:.2f} miles" if dist is not None else "unknown"
        text_lines += [
            f"Reference : {app['reference']}",
            f"Address   : {app['address']}",
            f"Distance  : {dist_str}",
            f"Description: {app['description']}",
            f"URL       : {app['url']}",
            "",
        ]
        html_rows.append(
            f"<tr>"
            f"<td><a href=\"{app['url']}\">{app['reference']}</a></td>"
            f"<td>{app['address']}</td>"
            f"<td style='text-align:center'>{dist_str}</td>"
            f"<td>{app['description']}</td>"
            f"</tr>"
        )

    date_str = datetime.now().strftime("%d %B %Y")
    rows_html = "".join(html_rows)
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;color:#222">
<h2 style="color:#1a5276">North Devon Planning Alert</h2>
<p>{count} new application{'s' if count != 1 else ''} within
<strong>{RADIUS_MILES:.0f} mile</strong> of <strong>{HOME_POSTCODE}</strong>
&mdash; {date_str}</p>
<table border="1" cellpadding="8" cellspacing="0"
  style="border-collapse:collapse;width:100%;font-size:14px">
  <thead style="background:#1a5276;color:#fff">
    <tr>
      <th>Reference</th><th>Address</th><th>Distance</th><th>Description</th>
    </tr>
  </thead>
  <tbody>{rows_html}</tbody>
</table>
<p style="font-size:12px;color:#777;margin-top:20px">
  <a href="https://planning.northdevon.gov.uk/">North Devon Planning Portal</a>
</p>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = email_from
    msg["To"]      = email_to
    msg.attach(MIMEText("\n".join(text_lines), "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(smtp_host, smtp_port) as srv:
        srv.ehlo()
        srv.starttls()
        srv.login(email_from, email_password)
        srv.sendmail(email_from, email_to, msg.as_string())

    print(f"Email sent -> {email_to}: {subject}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="North Devon planning notifier")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--dump-html", action="store_true")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument(
        "--strategy",
        choices=["1", "2", "3", "auto"],
        default="auto",
        help="1=viewer, 2=GovDelivery, 3=portal search, auto=try all",
    )
    args = parser.parse_args()

    print(f"[{datetime.now().isoformat()}] North Devon planning check")
    print(f"  Home: {HOME_POSTCODE} ({HOME_LAT}, {HOME_LON})  radius: {RADIUS_MILES} mi")
    print()

    all_apps = []

    if args.strategy in ("1", "auto"):
        all_apps = fetch_via_viewer(dump_html=args.dump_html)

    if not all_apps and args.strategy in ("2", "auto"):
        all_apps = fetch_via_govdelivery(dump_html=args.dump_html)

    if not all_apps and args.strategy in ("3", "auto"):
        all_apps = fetch_via_portal_search(days_back=args.days, dump_html=args.dump_html)

    if not all_apps:
        print("No applications found by any strategy.")
        sys.exit(0)

    print(f"\nFetched {len(all_apps)} application(s) total")

    seen     = load_seen()
    new_seen = set(seen)
    nearby   = []

    for app in all_apps:
        ref = app["reference"]
        if ref in seen:
            continue
        new_seen.add(ref)

        address = app.get("address", "")
        if not address:
            print(f"  [{ref}] no address – skipping geocode")
            continue

        lat, lon = geocode_address(address)
        if lat is None:
            print(f"  [{ref}] could not geocode: {address!r}")
            continue

        dist = haversine_miles(HOME_LAT, HOME_LON, lat, lon)
        app["distance_miles"] = dist

        if dist <= RADIUS_MILES:
            print(f"  NEARBY  {dist:.2f} mi  {ref}  {address}")
            nearby.append(app)
        else:
            print(f"  far     {dist:.2f} mi  {ref}")

    nearby.sort(key=lambda a: a.get("distance_miles", 999))
    print(f"\n{len(nearby)} nearby new application(s).")

    if args.dry_run:
        print("(dry-run: skipping email and state update)")
        return

    save_seen(new_seen)

    if nearby:
        send_email(nearby)
    else:
        print("Nothing nearby – no email sent.")


if __name__ == "__main__":
    main()
