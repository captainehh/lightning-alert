#!/usr/bin/env python3
"""
Ford Lightning daily alert.

Once a day this checks MarketCheck's active US inventory (both dealer and
private-seller listings) for a used F-150 Lightning that matches the criteria
below, keeps only the ones with the Extended Range battery, drops anything it
already emailed about, and emails a digest of what's new.

Search criteria live in the CONFIG block. Secrets come from environment
variables (set as GitHub Actions secrets):
  MARKETCHECK_API_KEY
  GMAIL_USER
  GMAIL_APP_PASSWORD
  ALERT_TO
Optional:
  INSPECT=1        print the raw fields of the first listing (for tuning)
  ALWAYS_EMAIL=1   email every day even when there's nothing new
"""

import json
import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# CONFIG  --  edit here if the search ever changes
# ---------------------------------------------------------------------------
MAKE = "Ford"
MODEL = "F-150 Lightning"
YEARS = [2022, 2023, 2024, 2025]
PRICE_MAX = 52000
MILES_MAX = 40000
# Nationwide, so no location/radius filter is applied.

# Both endpoints get searched and the results merged + deduped by VIN.
ENDPOINTS = [
    ("dealer", "https://api.marketcheck.com/v2/search/car/active"),
    ("private", "https://api.marketcheck.com/v2/search/car/fsbo/active"),
]
PAGE_SIZE = 50  # max rows per call on the standard search API
SEEN_FILE = Path("seen.json")  # remembers what we've already emailed

INSPECT = os.environ.get("INSPECT") == "1"
ALWAYS_EMAIL = os.environ.get("ALWAYS_EMAIL") == "1"


def env(name):
    val = os.environ.get(name)
    if not val:
        sys.exit(f"Missing required environment variable: {name}")
    return val


def search_endpoint(url, api_key):
    """Pull every matching listing from one endpoint, paging through results."""
    out = []
    start = 0
    while True:
        params = {
            "api_key": api_key,
            "make": MAKE,
            "model": MODEL,
            "year": ",".join(str(y) for y in YEARS),
            "price_range": f"0-{PRICE_MAX}",
            "miles_range": f"0-{MILES_MAX}",
            "car_type": "used",
            "rows": PAGE_SIZE,
            "start": start,
        }
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        batch = data.get("listings", []) or []
        out.extend(batch)

        num_found = data.get("num_found", 0)
        start += PAGE_SIZE
        if start >= num_found or not batch:
            break
    return out


def get_all_listings(api_key):
    merged = {}
    for source, url in ENDPOINTS:
        try:
            found = search_endpoint(url, api_key)
        except requests.HTTPError as e:
            # A plan might not include one endpoint; don't let it kill the run.
            print(f"  ({source} search skipped: {e})")
            continue
        print(f"  {source}: {len(found)} listings")
        for lst in found:
            lst["_source"] = source
            merged[listing_id(lst)] = lst  # dedupe by VIN/id across sources
    return list(merged.values())


def is_extended_range(listing):
    """
    Best-effort Extended Range check.

    ER is the ~131 kWh pack (~320 mi). Standard on Platinum and the 2024+
    Flash, optional on XLT/Lariat, not offered on the base Pro. We look at
    trim + any range/text signal MarketCheck returns. Run once with INSPECT=1
    to see the real fields, then this can be locked to the exact one.
    """
    build = listing.get("build", {}) or {}
    trim = (build.get("trim") or "").lower()
    heading = (listing.get("heading") or "").lower()
    haystack = f"{trim} {heading}"

    if "platinum" in trim or "flash" in trim:
        return True
    if any(k in haystack for k in ("extended range", "ext range", "131 kwh", "ext. range")):
        return True
    for key in ("electric_range", "epa_range", "range"):
        val = build.get(key)
        try:
            if val and float(val) >= 280:
                return True
        except (TypeError, ValueError):
            pass
    return False


def listing_id(listing):
    return listing.get("vin") or listing.get("id") or listing.get("listing_id")


def get_location(listing):
    for block in ("dealer", "seller"):
        b = listing.get(block, {}) or {}
        city, state = b.get("city"), b.get("state")
        if city or state:
            return ", ".join(p for p in (city, state) if p)
    city, state = listing.get("city"), listing.get("state")
    return ", ".join(p for p in (city, state) if p) or "—"


def get_url(listing):
    for key in ("vdp_url", "source_url", "url"):
        if listing.get(key):
            return listing[key]
    return "#"


def load_seen():
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except json.JSONDecodeError:
            return set()
    return set()


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(sorted(str(s) for s in seen)))


def format_row(listing):
    build = listing.get("build", {}) or {}
    year = build.get("year", "")
    trim = build.get("trim", "")
    price = listing.get("price")
    miles = listing.get("miles")
    src = "Private seller" if listing.get("_source") == "private" else "Dealer"

    price_s = f"${int(price):,}" if price else "—"
    miles_s = f"{int(miles):,} mi" if miles else "—"

    cell = "padding:8px;border-bottom:1px solid #eee;"
    return (
        f"<tr><td style='{cell}'>{year} F-150 Lightning {trim}</td>"
        f"<td style='{cell}'>{price_s}</td>"
        f"<td style='{cell}'>{miles_s}</td>"
        f"<td style='{cell}'>{get_location(listing)}</td>"
        f"<td style='{cell}'>{src}</td>"
        f"<td style='{cell}'><a href='{get_url(listing)}'>View</a></td></tr>"
    )


def format_email(new_listings):
    # Cheapest first
    new_listings = sorted(new_listings, key=lambda l: l.get("price") or 1e12)
    rows = "".join(format_row(l) for l in new_listings)
    n = len(new_listings)
    head = "padding:8px;border-bottom:2px solid #333;text-align:left;"
    return (
        "<html><body style='font-family:Arial,sans-serif;color:#222;'>"
        f"<h2>{n} new Lightning match{'' if n == 1 else 'es'}</h2>"
        f"<p>Used F-150 Lightning, Extended Range, {YEARS[0]}-{YEARS[-1]}, "
        f"under ${PRICE_MAX:,} and {MILES_MAX:,} miles, nationwide.</p>"
        "<table style='border-collapse:collapse;width:100%;'>"
        f"<tr><th style='{head}'>Vehicle</th><th style='{head}'>Price</th>"
        f"<th style='{head}'>Miles</th><th style='{head}'>Location</th>"
        f"<th style='{head}'>Source</th><th style='{head}'>Link</th></tr>"
        f"{rows}</table></body></html>"
    )


def send_email(html, subject):
    user, pw, to = env("GMAIL_USER"), env("GMAIL_APP_PASSWORD"), env("ALERT_TO")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(user, pw)
        server.sendmail(user, [to], msg.as_string())


def main():
    api_key = env("MARKETCHECK_API_KEY")

    print("Searching...")
    listings = get_all_listings(api_key)
    print(f"Total unique listings in range: {len(listings)}")

    if INSPECT and listings:
        print("First listing raw fields:")
        print(json.dumps(listings[0], indent=2)[:4000])

    er = [l for l in listings if is_extended_range(l)]
    print(f"Extended Range matches: {len(er)}")

    seen = load_seen()
    new = [l for l in er if listing_id(l) not in seen]
    print(f"New since last run: {len(new)}")

    if new:
        send_email(format_email(new), f"{len(new)} new Lightning match(es)")
        seen.update(listing_id(l) for l in new)
        save_seen(seen)
        print("Email sent.")
    elif ALWAYS_EMAIL:
        send_email("<p>No new Lightning matches today.</p>", "Lightning: nothing new")
        print("Email sent (nothing new).")
    else:
        print("Nothing new; no email.")


if __name__ == "__main__":
    main()
