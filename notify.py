#!/usr/bin/env python3
"""
North Devon Planning Notifications

Fetches newly registered planning applications from North Devon's planning portal,
filters those within RADIUS_MILES of home (EX33 2LD, Braunton), and sends an email.

Run via GitHub Actions weekly, or manually:
    python notify.py
    python notify.py --dry-run   # print matches, skip email and state update
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

# EX33 2LD – centre of Braunton village, North Devon
HOME_LAT = 51.10264
HOME_LON = -4.16247
HOME_POSTCODE = "EX33 2LD"
RADIUS_MILES = float(os.getenv("RADIUS_MILES", "1"))

# North Devon planning portal (IDOX Uniform)
PLANNING_BASE = os.getenv(
    "PLANNING_BASE", "https://planning.northdevon.gov.uk"
)
SEARCH_PATH = "/Planning/search"

STATE_FILE = "state.json"

POSTCODES_IO = "https://api.postcodes.io/postcodes/{}"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# Browser-like headers to avoid bot detection
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

POSTCODE_RE = re.compile(
    r"\b([A-Z]{1,2}\d{1,2}[A-Z]?\s*\d[A-Z]{2})\b", re.IGNORECASE
)

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
    """Look up a UK postcode via postcodes.io (free, no key required)."""
    clean = postcode.strip().upper().replace(" ", "")
    try:
        resp = requests.get(POSTCODES_IO.format(clean), timeout=10)
        if resp.ok:
            data = resp.json().get("result", {})
            return data.get("latitude"), data.get("longitude")
    except Exception:
        pass
    return None, None


def geocode_nominatim(address: str) -> tuple[float | None, float | None]:
    """Geocode a free-text address via Nominatim (OSM). Rate-limited to 1 req/s."""
    time.sleep(1)
    params = {
        "q": f"{address}, North Devon, UK",
        "format": "json",
        "limit": 1,
        "countrycodes": "gb",
    }
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params=params,
            headers={"User-Agent": "NorthDevonPlanningNotifier/1.0 rob@medberry.co.uk"},
            timeout=15,
        )
        results = resp.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception:
        pass
    return None, None


def geocode_address(address: str) -> tuple[float | None, float | None]:
    """
    Try to geocode an address.
    1. Extract postcode and hit postcodes.io (fast, accurate).
    2. Fall back to Nominatim free-text search.
    """
    match = POSTCODE_RE.search(address)
    if match:
        lat, lon = geocode_postcode(match.group(0))
        if lat is not None:
            return lat, lon
    return geocode_nominatim(address)


# ---------------------------------------------------------------------------
# Planning portal scraper
# ---------------------------------------------------------------------------


def fetch_applications(days_back: int = 7) -> list[dict]:
    """
    Fetch newly registered planning applications from the IDOX portal.
    Returns a list of application dicts.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    # Prime the session (get cookies) by visiting the base search page
    try:
        session.get(f"{PLANNING_BASE}{SEARCH_PATH}", timeout=20)
    except requests.RequestException:
        pass  # Continue anyway; cookies may not be required

    week_ago = (datetime.now() - timedelta(days=days_back)).strftime("%d/%m/%Y")
    today = datetime.now().strftime("%d/%m/%Y")

    applications: list[dict] = []
    page = 1

    while True:
        params = {
            "searchType": "Application",
            "applicationType": "",
            "applicationStatus": "newly+registered",
            "dateType": "DC_Received",
            "date(applicationReceivedStart)": week_ago,
            "date(applicationReceivedEnd)": today,
            "searchCriteria.page": page,
            "searchCriteria.resultsPerPage": 100,
        }

        try:
            resp = session.get(
                f"{PLANNING_BASE}{SEARCH_PATH}", params=params, timeout=30
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[fetch] Error on page {page}: {exc}", file=sys.stderr)
            break

        page_apps, has_more = _parse_idox_page(resp.text)
        applications.extend(page_apps)

        if not page_apps or not has_more:
            break
        page += 1

    return applications


def _parse_idox_page(html: str) -> tuple[list[dict], bool]:
    """Parse IDOX search results HTML. Returns (applications, has_next_page)."""
    soup = BeautifulSoup(html, "lxml")
    applications = []

    # IDOX renders results as <li class="searchresult"> items
    results = soup.find_all("li", class_=re.compile(r"searchresult", re.I))

    for item in results:
        try:
            ref_tag = (
                item.find("a", class_=re.compile(r"reference", re.I))
                or item.find("a")
            )
            addr_tag = item.find("p", class_=re.compile(r"address", re.I))
            desc_tag = item.find("p", class_=re.compile(r"description|summary", re.I))
            status_tag = item.find(
                attrs={"class": re.compile(r"status|applicationStatus", re.I)}
            )
            date_tag = item.find(
                attrs={"class": re.compile(r"date|received", re.I)}
            )

            ref = ref_tag.get_text(strip=True) if ref_tag else ""
            href = ref_tag.get("href", "") if ref_tag else ""
            if not ref:
                continue

            url = (
                f"{PLANNING_BASE}{href}"
                if href.startswith("/")
                else href or f"{PLANNING_BASE}{SEARCH_PATH}"
            )

            applications.append(
                {
                    "reference": ref,
                    "address": addr_tag.get_text(strip=True) if addr_tag else "",
                    "description": desc_tag.get_text(strip=True) if desc_tag else "",
                    "status": status_tag.get_text(strip=True) if status_tag else "",
                    "date_received": date_tag.get_text(strip=True) if date_tag else "",
                    "url": url,
                }
            )
        except Exception as exc:
            print(f"[parse] Skipping result: {exc}", file=sys.stderr)

    # Next-page link signals there are more results
    next_link = soup.find("a", string=re.compile(r"next", re.I)) or soup.find(
        "a", class_=re.compile(r"next", re.I)
    )
    return applications, next_link is not None


# ---------------------------------------------------------------------------
# State (seen application IDs)
# ---------------------------------------------------------------------------


def load_seen() -> set[str]:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as fh:
            return set(json.load(fh).get("seen", []))
    return set()


def save_seen(seen: set[str]) -> None:
    with open(STATE_FILE, "w") as fh:
        json.dump(
            {"seen": sorted(seen), "updated": datetime.utcnow().isoformat() + "Z"},
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
<html><head><meta charset="utf-8"></head><body style="font-family:Arial,sans-serif;color:#222">
<h2 style="color:#1a5276">North Devon Planning Alert</h2>
<p>{count} new application{'s' if count != 1 else ''} within
<strong>{RADIUS_MILES:.0f} mile</strong> of <strong>{HOME_POSTCODE}</strong>
&mdash; {date_str}</p>
<table border="1" cellpadding="8" cellspacing="0"
  style="border-collapse:collapse;width:100%;font-size:14px">
  <thead style="background:#1a5276;color:#fff">
    <tr>
      <th>Reference</th>
      <th>Address</th>
      <th>Distance</th>
      <th>Description</th>
    </tr>
  </thead>
  <tbody>
    {"".join(html_rows)}
  </tbody>
</table>
<p style="font-size:12px;color:#777;margin-top:20px">
  Source: <a href="{PLANNING_BASE}">North Devon Planning Portal</a>
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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print matches but do not send email or update state",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Look back this many days (default 7)",
    )
    args = parser.parse_args()

    print(f"[{datetime.now().isoformat()}] Checking North Devon planning applications")
    print(f"  Home: {HOME_POSTCODE} ({HOME_LAT}, {HOME_LON})")
    print(f"  Radius: {RADIUS_MILES} mile(s)  |  Days back: {args.days}")
    print()

    seen = load_seen()
    all_apps = fetch_applications(days_back=args.days)
    print(f"Fetched {len(all_apps)} application(s) from portal")

    new_seen = set(seen)
    nearby: list[dict] = []

    for app in all_apps:
        ref = app["reference"]
        if ref in seen:
            continue
        new_seen.add(ref)

        address = app["address"]
        if not address:
            print(f"  [{ref}] no address – skipping")
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

    print(f"\n{len(nearby)} nearby new application(s) found.")

    if args.dry_run:
        print("(dry-run: skipping email and state update)")
        return

    if not args.dry_run:
        save_seen(new_seen)

    if nearby:
        send_email(nearby)


if __name__ == "__main__":
    main()
