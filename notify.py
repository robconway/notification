#!/usr/bin/env python3
"""
North Devon Planning Notifications

Strategy 1 (primary): Fetch the weekly planning report viewer from
  my.northdevon.gov.uk using a real browser (Playwright).

Strategy 2 (fallback): Fetch the latest GovDelivery bulletin email
  that North Devon publishes to content.govdelivery.com.

Applications within RADIUS_MILES of home (EX33 2LD, Braunton) trigger
an email.  Seen application references are stored in state.json so you
only get notified once per application.

Run manually:
    python notify.py
    python notify.py --dry-run        # no email, no state update
    python notify.py --dump-html      # save raw HTML for debugging
    python notify.py --days 14        # look back further (strategy 2 only)
"""

import argparse
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from math import atan2, cos, radians, sin, sqrt

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

# Weekly planning report viewer (the URL North Devon emails you)
VIEWER_URL = (
    "https://my.northdevon.gov.uk/service/Weekly_planning_report_viewer"
    "?fromemail=newly%20registered"
)

# GovDelivery account for North Devon (fallback)
GOVDELIVERY_ACCOUNT = "UKNORTHDEVON"

# Individual application base URL on the planning portal
PLANNING_DISPLAY_URL = "https://planning.northdevon.gov.uk/Planning/Display/{ref}"

STATE_FILE = "state.json"
POSTCODES_IO = "https://api.postcodes.io/postcodes/{}"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d{1,2}[A-Z]?\s*\d[A-Z]{2})\b", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Distance
# ---------------------------------------------------------------------------


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 3958.8 * 2 * atan2(sqrt(a), sqrt(1 - a))


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------


def geocode_postcode(postcode: str) -> tuple[float | None, float | None]:
    clean = postcode.strip().upper().replace(" ", "")
    try:
        r = requests.get(POSTCODES_IO.format(clean), timeout=10)
        if r.ok:
            d = r.json().get("result", {})
            return d.get("latitude"), d.get("longitude")
    except Exception:
        pass
    return None, None


def geocode_nominatim(address: str) -> tuple[float | None, float | None]:
    time.sleep(1)  # Nominatim rate limit: 1 req/s
    params = {
        "q": f"{address}, North Devon, UK",
        "format": "json",
        "limit": 1,
        "countrycodes": "gb",
    }
    try:
        r = requests.get(
            NOMINATIM_URL,
            params=params,
            headers={"User-Agent": "NorthDevonPlanningNotifier/1.0 rob@medberry.co.uk"},
            timeout=15,
        )
        results = r.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception:
        pass
    return None, None


def geocode_address(address: str) -> tuple[float | None, float | None]:
    m = POSTCODE_RE.search(address)
    if m:
        lat, lon = geocode_postcode(m.group(0))
        if lat is not None:
            return lat, lon
    return geocode_nominatim(address)


# ---------------------------------------------------------------------------
# Parsing helpers (shared between both strategies)
# ---------------------------------------------------------------------------


def _extract_ref_from_text(text: str) -> str | None:
    """Try to find a North Devon planning reference in free text."""
    # North Devon uses numeric IDs like 81617 or refs like 81617 / 73108
    m = re.search(r"\b7\d{4}\b|\b8\d{4}\b", text)
    return m.group(0) if m else None


def parse_applications_from_html(html: str, source_label: str = "") -> list[dict]:
    """
    Parse planning applications out of an HTML page or email.

    Tries several common patterns:
     - /Planning/Display/<ref> links in href attributes  (most reliable)
     - Table rows with ref / address / description columns
     - <li> items with a reference and address
     - Any element containing a recognisable planning reference

    Returns a list of dicts: reference, address, description, url
    """
    soup = BeautifulSoup(html, "lxml")
    applications: list[dict] = []
    seen_refs: set[str] = set()

    # ---- Pattern 0: planning portal Display URLs embedded in hrefs ----
    # Catches e.g. <a href=".../Planning/Display/80123">View</a>
    _display_re = re.compile(r"/[Dd]isplay/(\d{4,6})", re.IGNORECASE)
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        m = _display_re.search(href)
        if not m:
            continue
        ref = m.group(1)
        if ref in seen_refs:
            continue
        seen_refs.add(ref)
        parent = a.parent
        surrounding = parent.get_text(" ", strip=True) if parent else a.get_text(strip=True)
        url = href if href.startswith("http") else f"https://planning.northdevon.gov.uk{href}"
        applications.append({
            "reference": ref,
            "address": surrounding[:300],
            "description": "",
            "url": url,
            "source": source_label,
        })

    # ---- Pattern 1: table rows ----
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for row in rows[1:]:  # skip header
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            texts = [c.get_text(strip=True) for c in cells]
            ref = _extract_ref_from_text(texts[0]) or _extract_ref_from_text(
                " ".join(texts)
            )
            if not ref or ref in seen_refs:
                continue
            seen_refs.add(ref)
            address = texts[1] if len(texts) > 1 else ""
            description = texts[2] if len(texts) > 2 else ""
            link = cells[0].find("a")
            url = link.get("href", "") if link else PLANNING_DISPLAY_URL.format(ref=ref)
            if url.startswith("/"):
                url = "https://my.northdevon.gov.uk" + url
            applications.append(
                {
                    "reference": ref,
                    "address": address,
                    "description": description,
                    "url": url,
                    "source": source_label,
                }
            )

    # ---- Pattern 2: list items / divs with ref + address ----
    if not applications:
        for item in soup.find_all(["li", "div", "p"]):
            text = item.get_text(" ", strip=True)
            ref = _extract_ref_from_text(text)
            if not ref or ref in seen_refs:
                continue
            seen_refs.add(ref)
            link = item.find("a")
            url = ""
            if link:
                url = link.get("href", "")
                if url.startswith("/"):
                    url = "https://my.northdevon.gov.uk" + url
            applications.append(
                {
                    "reference": ref,
                    "address": text[:200],
                    "description": "",
                    "url": url or PLANNING_DISPLAY_URL.format(ref=ref),
                    "source": source_label,
                }
            )

    # ---- Pattern 3: every hyperlink whose text looks like a reference ----
    if not applications:
        for a in soup.find_all("a", href=True):
            ref = _extract_ref_from_text(a.get_text(strip=True))
            if ref and ref not in seen_refs:
                seen_refs.add(ref)
                parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""
                url = a["href"]
                if url.startswith("/"):
                    url = "https://planning.northdevon.gov.uk" + url
                applications.append(
                    {
                        "reference": ref,
                        "address": parent_text[:200],
                        "description": "",
                        "url": url,
                        "source": source_label,
                    }
                )

    return applications


# ---------------------------------------------------------------------------
# Strategy 1: Playwright (real browser) fetch of the weekly viewer
# ---------------------------------------------------------------------------


def fetch_via_playwright(dump_html: bool = False) -> list[dict]:
    """
    Launch a headless Chromium browser and load the weekly planning
    report viewer.  Requires: pip install playwright && playwright install chromium
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[playwright] not installed – skipping strategy 1", file=sys.stderr)
        return []

    print("[strategy 1] Fetching weekly report via Playwright …")
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
            page.goto(VIEWER_URL, wait_until="networkidle", timeout=30_000)

            # If we land on a login page, try credentials
            if page.url != VIEWER_URL and "login" in page.url.lower():
                username = os.getenv("PORTAL_USERNAME", "")
                password = os.getenv("PORTAL_PASSWORD", "")
                if username and password:
                    print("[strategy 1] Login page detected – attempting login …")
                    page.fill('input[type="email"], input[name*="user"], input[name*="email"]', username)
                    page.fill('input[type="password"]', password)
                    page.click('button[type="submit"], input[type="submit"]')
                    page.wait_for_load_state("networkidle", timeout=15_000)
                    # Navigate back to the viewer after login
                    page.goto(VIEWER_URL, wait_until="networkidle", timeout=30_000)
                else:
                    print(
                        "[strategy 1] Login required but no PORTAL_USERNAME/PORTAL_PASSWORD set",
                        file=sys.stderr,
                    )

            html = page.content()
            browser.close()
    except Exception as e:
        print(f"[strategy 1] Playwright error: {e}", file=sys.stderr)
        return []

    if dump_html:
        with open("debug_strategy1.html", "w") as fh:
            fh.write(html)
        print("[strategy 1] HTML saved to debug_strategy1.html")

    apps = parse_applications_from_html(html, source_label="viewer")
    print(f"[strategy 1] Parsed {len(apps)} application(s)")
    if not apps:
        snippet = html.replace("\n", " ").replace("\r", "")[:3000]
        print(f"[strategy 1] Viewer HTML excerpt (first 3000 chars):\n{snippet}", file=sys.stderr)
    return apps


# ---------------------------------------------------------------------------
# Strategy 2: GovDelivery bulletin
# ---------------------------------------------------------------------------

# Known bulletin hex IDs from search results (most recent first).
# The script tries these and any IDs it can find by searching forward.
_KNOWN_BULLETIN_IDS = [
    "3daba2e",  # "Recently registered planning applications Update" (~2025)
    "3b48b71",  # 06 Sep 2024 recently registered
    "39efbdb",  # 24 May 2024 recently registered
    "3863cc4",  # 19 Jan 2024 recently registered
    "380872a",  # 15 Dec 2023
]

_GOVDELIVERY_BASE = "https://content.govdelivery.com/accounts/UKNORTHDEVON/bulletins/{}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-GB,en;q=0.9",
}


def _bulletin_url(hex_id: str) -> str:
    return _GOVDELIVERY_BASE.format(hex_id)


def _fetch_bulletin_html(hex_id: str) -> str | None:
    url = _bulletin_url(hex_id)
    try:
        r = requests.get(url, headers=_HEADERS, timeout=20)
        if r.ok and "planning" in r.text.lower():
            return r.text
    except Exception:
        pass
    return None


def _find_latest_bulletin() -> tuple[str | None, str | None]:
    """
    Return (hex_id, html) for the most recent 'newly registered' planning
    bulletin we can reach.

    Starts from the most recently known ID and searches forward
    (incrementing by ~87 000 per week) for up to 200 weeks ahead.
    Also falls back through the known list.
    """
    # Approximate GovDelivery-wide ID increment per week (empirically ~87 000)
    WEEK_INCREMENT = 87_000
    MAX_WEEKS_AHEAD = 200

    # Start point: most recent known ID
    start_hex = _KNOWN_BULLETIN_IDS[0]
    start_int = int(start_hex, 16)

    # Estimate how many weeks have passed since the start point (~Apr 2025)
    # so we jump close to the current week first
    start_date = datetime(2025, 4, 1)
    weeks_elapsed = max(0, int((datetime.now() - start_date).days / 7))

    candidates: list[int] = []

    # Look around the estimated current position
    for offset in range(-4, MAX_WEEKS_AHEAD):
        w = weeks_elapsed + offset
        if w < 0:
            continue
        candidates.append(start_int + w * WEEK_INCREMENT)

    # Also add all known IDs
    for known in _KNOWN_BULLETIN_IDS:
        candidates.insert(0, int(known, 16))

    # Remove duplicates and sort descending (newest first)
    candidates = sorted(set(candidates), reverse=True)

    for cand_int in candidates:
        hex_id = format(cand_int, "x")
        print(f"[strategy 2] Trying bulletin {hex_id} …")
        html = _fetch_bulletin_html(hex_id)
        if html:
            # Confirm it mentions registered (not decided) applications
            lower = html.lower()
            if any(phrase in lower for phrase in (
                "newly registered", "newly+registered",
                "recently registered", "recently+registered",
            )):
                print(f"[strategy 2] Found matching bulletin: {hex_id}")
                return hex_id, html
        time.sleep(0.3)

    return None, None


def fetch_via_govdelivery(
    days_back: int = 14, dump_html: bool = False
) -> list[dict]:
    """Fetch the latest North Devon 'newly registered' GovDelivery bulletin."""
    print("[strategy 2] Searching GovDelivery bulletins …")
    hex_id, html = _find_latest_bulletin()

    if not html:
        print("[strategy 2] Could not find a recent bulletin", file=sys.stderr)
        return []

    if dump_html:
        fname = f"debug_bulletin_{hex_id}.html"
        with open(fname, "w") as fh:
            fh.write(html)
        print(f"[strategy 2] HTML saved to {fname}")

    apps = parse_applications_from_html(
        html, source_label=f"bulletin/{hex_id}"
    )
    print(f"[strategy 2] Parsed {len(apps)} application(s)")
    if not apps:
        # Print a snippet so the GitHub Actions log shows what the bulletin contains
        snippet = html.replace("\n", " ").replace("\r", "")[:3000]
        print(f"[strategy 2] Bulletin excerpt (first 3000 chars):\n{snippet}", file=sys.stderr)
    return apps


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def load_seen() -> set[str]:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as fh:
            return set(json.load(fh).get("seen", []))
    return set()


def save_seen(seen: set[str]) -> None:
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


def send_email(applications: list[dict]) -> None:
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    email_from = os.environ["EMAIL_FROM"]
    email_password = os.environ["EMAIL_PASSWORD"]
    email_to = os.getenv("EMAIL_TO", "rob@medberry.co.uk")

    count = len(applications)
    subject = (
        f"Planning alert: {count} new application{'s' if count != 1 else ''} "
        f"within {RADIUS_MILES:.0f} mile of {HOME_POSTCODE}"
    )

    text_lines = [subject, "=" * len(subject), ""]
    html_rows = []

    for app in applications:
        dist = app.get("distance_miles")
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
    html = f"""<!DOCTYPE html>
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
  <tbody>{"".join(html_rows)}</tbody>
</table>
<p style="font-size:12px;color:#777;margin-top:20px">
  <a href="https://planning.northdevon.gov.uk/">North Devon Planning Portal</a>
</p>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to
    msg.attach(MIMEText("\n".join(text_lines), "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP(smtp_host, smtp_port) as srv:
        srv.ehlo()
        srv.starttls()
        srv.login(email_from, email_password)
        srv.sendmail(email_from, email_to, msg.as_string())

    print(f"Email sent → {email_to}: {subject}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="North Devon planning notifier")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print matches, skip email and state update")
    parser.add_argument("--dump-html", action="store_true",
                        help="Save raw HTML to files for debugging")
    parser.add_argument("--days", type=int, default=7,
                        help="Days to look back (GovDelivery strategy, default 7)")
    parser.add_argument("--strategy", choices=["1", "2", "auto"], default="auto",
                        help="1=Playwright viewer, 2=GovDelivery, auto=try both")
    args = parser.parse_args()

    print(f"[{datetime.now().isoformat()}] North Devon planning check")
    print(f"  Home: {HOME_POSTCODE} ({HOME_LAT}, {HOME_LON})  radius: {RADIUS_MILES} mi")
    print()

    all_apps: list[dict] = []

    if args.strategy in ("1", "auto"):
        all_apps = fetch_via_playwright(dump_html=args.dump_html)

    if not all_apps and args.strategy in ("2", "auto"):
        all_apps = fetch_via_govdelivery(days_back=args.days, dump_html=args.dump_html)

    if not all_apps:
        print("No applications fetched from either strategy.")
        print(
            "Tip: run with --dump-html and check debug_*.html to see what the "
            "portal returned.  If the viewer requires login, set "
            "PORTAL_USERNAME and PORTAL_PASSWORD secrets."
        )
        sys.exit(0)

    print(f"Fetched {len(all_apps)} application(s) total")

    seen = load_seen()
    new_seen = set(seen)
    nearby: list[dict] = []

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
