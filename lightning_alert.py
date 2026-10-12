#!/usr/bin/env python3
"""
Ford Lightning daily alert.

Once a day this checks MarketCheck's active US inventory (both dealer and
private-seller listings) for a used F-150 Lightning that matches Ash's
criteria, keeps only the right trims with the Extended Range battery, drops
anything it already emailed about, and emails a digest of what's new.

Target: 2022-2025 F-150 Lightning, Lariat / Platinum / Flash, Extended Range,
under $52k and 40k miles, nationwide. Pro Power Onboard (9.6 kW) is standard
on Lariat and Platinum; Flash matches are flagged to confirm it.

Secrets come from environment variables (set as GitHub Actions secrets):
  MARKETCHECK_API_KEY, GMAIL_USER, GMAIL_APP_PASSWORD, ALERT_TO
Optional:
  INSPECT=1        print the spec fields of the first listings (for tuning)
  ALWAYS_EMAIL=1   email every day even when there's nothing new
"""

import json
import os
import re
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
# Trims we want. Pro and XLT are intentionally left out.
TARGET_TRIMS = {"lariat", "platinum", "flash"}

ENDPOINTS = [
    ("dealer", "https://api.marketcheck.com/v2/search/car/active"),
    ("private", "https://api.marketcheck.com/v2/search/car/fsbo/active"),
]
PAGE_SIZE = 50
SEEN_FILE = Path("seen.json")

INSPECT = os.environ.get("INSPECT") == "1"
ALWAYS_EMAIL = os.environ.get("ALWAYS_EMAIL") == "1"


def env(name):
    val = os.environ.get(name)
    if not val:
        sys.exit(f"Missing required environment variable: {name}")
    return val


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def search_endpoint(url, api_key):
    out, start = [], 0
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
            print(f"  ({source} search skipped: {e})")
            continue
        print(f"  {source}: {len(found)} listings")
        for lst in found:
            lst["_source"] = source
            merged[listing_id(lst)] = lst
    return list(merged.values())


# ---------------------------------------------------------------------------
# Field helpers (defensive — spec fields live in different places by source)
# ---------------------------------------------------------------------------
def _build(listing):
    return listing.get("build", {}) or {}


def get_trim(listing):
    trim = (_build(listing).get("trim") or "").strip().lower()
    if trim:
        for t in ("platinum", "lariat", "flash", "xlt", "pro"):
            if t in trim:
                return t
    heading = (listing.get("heading") or "").lower()
    for t in ("platinum", "lariat", "flash", "xlt", "pro"):
        if t in heading:
            return t
    return ""


def get_year(listing):
    y = _build(listing).get("year")
    try:
        return int(y)
    except (TypeError, ValueError):
        pass
    m = re.search(r"\b(20\d{2})\b", listing.get("heading") or "")
    return int(m.group(1)) if m else None


def _first_number(listing, keys):
    for src in (_build(listing), listing):
        for k in keys:
            v = src.get(k)
            if v is None:
                continue
            try:
                return float(str(v).split()[0].replace(",", ""))
            except (ValueError, IndexError):
                continue
    return None


def er_signal(listing):
    """Extended Range read from the data: 'yes', 'no', or 'unknown'."""
    b = _build(listing)
    text = " ".join([
        listing.get("heading") or "",
        b.get("trim") or "",
        b.get("version") or "",
    ]).lower()

    if any(s in text for s in ("extended range", "ext range", "ext. range", "131 kwh", "131kwh")):
        return "yes"
    if any(s in text for s in ("standard range", "std range", "98 kwh", "98kwh")):
        return "no"

    hp = _first_number(listing, ("horsepower", "engine_hp"))
    if hp is not None:
        if hp >= 520:
            return "yes"
        if hp <= 480:
            return "no"

    rng = _first_number(listing, ("electric_range", "epa_range", "ev_range", "range"))
    if rng is not None:
        if rng >= 280:
            return "yes"
        if rng <= 260:
            return "no"

    kwh = _first_number(listing, ("battery_capacity", "battery_kwh"))
    if kwh is not None:
        if kwh >= 120:
            return "yes"
        if kwh <= 110:
            return "no"

    return "unknown"


def evaluate(listing):
    """(include, note) per Ash's criteria. note='' means a clean match."""
    trim = get_trim(listing)
    if trim not in TARGET_TRIMS:
        return (False, "")

    year = get_year(listing)
    sig = er_signal(listing)

    if trim == "platinum":          # always Extended Range
        return (True, "")

    if trim == "flash":             # ER standard; PPO level not in the data
        if sig == "no":
            return (False, "")
        return (True, "verify 9.6kW Pro Power Onboard")

    if trim == "lariat":
        if year and year >= 2024:   # ER standard on 2024-25 Lariat
            return (True, "")
        if sig == "yes":
            return (True, "")
        if sig == "no":             # confirmed Standard Range -> drop
            return (False, "")
        return (True, "battery unconfirmed - verify Extended Range")

    return (False, "")


# ---------------------------------------------------------------------------
# Identity, location, links, state
# ---------------------------------------------------------------------------
def listing_id(listing):
    return listing.get("vin") or listing.get("id") or listing.get("listing_id")


def get_location(listing):
    for block in ("dealer", "seller"):
        b = listing.get(block, {}) or {}
        city, state = b.get("city"), b.get("state")
        if city or state:
            return ", ".join(p for p in (city, state) if p)
    city, state = listing.get("city"), listing.get("state")
    return ", ".join(p for p in (city, state) if p) or "-"


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


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def format_row(listing):
    b = _build(listing)
    year = b.get("year") or get_year(listing) or ""
    trim = (get_trim(listing) or "").title()
    price = listing.get("price")
    miles = listing.get("miles")
    src = "Private seller" if listing.get("_source") == "private" else "Dealer"
    note = listing.get("_note", "")

    price_s = f"${int(price):,}" if price else "-"
    miles_s = f"{int(miles):,} mi" if miles else "-"
    cell = "padding:8px;border-bottom:1px solid #eee;"
    note_cell = cell + ("color:#b35c00;" if note else "")
    return (
        f"<tr><td style='{cell}'>{year} Lightning {trim}</td>"
        f"<td style='{cell}'>{price_s}</td>"
        f"<td style='{cell}'>{miles_s}</td>"
        f"<td style='{cell}'>{get_location(listing)}</td>"
        f"<td style='{cell}'>{src}</td>"
        f"<td style='{note_cell}'>{note}</td>"
        f"<td style='{cell}'><a href='{get_url(listing)}'>View</a></td></tr>"
    )


def format_email(new_listings):
    new_listings = sorted(new_listings, key=lambda l: l.get("price") or 1e12)
    rows = "".join(format_row(l) for l in new_listings)
    n = len(new_listings)
    head = "padding:8px;border-bottom:2px solid #333;text-align:left;"
    return (
        "<html><body style='font-family:Arial,sans-serif;color:#222;'>"
        f"<h2>{n} new Lightning match{'' if n == 1 else 'es'}</h2>"
        f"<p>Used F-150 Lightning, Lariat / Platinum / Flash, Extended Range, "
        f"{YEARS[0]}-{YEARS[-1]}, under ${PRICE_MAX:,} and {MILES_MAX:,} miles, "
        f"nationwide. Anything in the Verify column needs a quick look on the "
        f"listing before you get excited.</p>"
        "<table style='border-collapse:collapse;width:100%;'>"
        f"<tr><th style='{head}'>Vehicle</th><th style='{head}'>Price</th>"
        f"<th style='{head}'>Miles</th><th style='{head}'>Location</th>"
        f"<th style='{head}'>Source</th><th style='{head}'>Verify</th>"
        f"<th style='{head}'>Link</th></tr>"
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    api_key = env("MARKETCHECK_API_KEY")

    print("Searching...")
    listings = get_all_listings(api_key)
    print(f"Total unique listings in range: {len(listings)}")

    if INSPECT and listings:
        sample = {k: v for k, v in listings[0].items() if k != "media"}
        print("First listing (photos stripped):")
        print(json.dumps(sample, indent=2)[:3500])
        print("\nWhat the filter sees on the first 15:")
        for l in listings[:15]:
            inc, note = evaluate(l)
            print(f"  {get_year(l)} {get_trim(l):8} ER={er_signal(l):7} "
                  f"keep={inc} {note} | {(l.get('heading') or '')[:55]}")

    matches = []
    for l in listings:
        include, note = evaluate(l)
        if include:
            l["_note"] = note
            matches.append(l)
    clean = sum(1 for l in matches if not l["_note"])
    print(f"Target matches: {len(matches)} ({clean} clean, {len(matches) - clean} to verify)")

    seen = load_seen()
    new = [l for l in matches if listing_id(l) not in seen]
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
