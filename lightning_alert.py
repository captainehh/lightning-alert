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
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

# NHTSA VIN decoder (free, no key). We only need it to confirm the battery on
# 2022-2023 Lariats, where Extended Range was optional. Platinum (all years),
# Flash (2024-25), and 2024-25 Lariat are Extended Range by trim/year.
VPIC_BATCH_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVINValuesBatch/"

# ---------------------------------------------------------------------------
# CONFIG  --  edit here if the search ever changes
# ---------------------------------------------------------------------------
MAKE = "Ford"
MODEL = "F-150 Lightning"
YEARS = [2022, 2023, 2024, 2025]
PRICE_MAX = 52000            # general ceiling
PRICE_MAX_BY_YEAR = {2025: 60000}   # 2025 gets a higher cap to catch a unicorn
MILES_MAX = 40000
# Only listings MarketCheck has re-seen within this many days. When a truck
# sells, the source drops it and it falls out of this window in a day or two,
# so this filters out most already-sold-but-still-listed cars at the source.
LAST_SEEN_MAX_DAYS = 3
# Trims we want. Pro and XLT are intentionally left out.
TARGET_TRIMS = {"lariat", "platinum", "flash"}

# Title-brand words that get a truck dropped (free text scan — only catches
# listings that actually say it). Paid per-VIN title history was declined.
BAD_TITLE_WORDS = (
    "salvage", "rebuilt", "rebuild", "branded title", "branded",
    "flood", "lemon", "junk", "total loss", "totaled", "dismantled",
    "not actual mileage", "odometer rollback", "bill of sale only",
)

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
            # Search the widest cap; per-year price is enforced in evaluate().
            "price_range": f"0-{max([PRICE_MAX] + list(PRICE_MAX_BY_YEAR.values()))}",
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


def decode_batteries(vins):
    """VIN -> 'yes'/'no'/'unknown' for Extended Range, via NHTSA (free, no key).

    The listing feed carries no battery spec, so we read it from the VIN.
    NHTSA's BatteryInfo says 'Extended Range Battery' or 'Standard Range
    Battery' in plain text for 2022-2024; 2025 uses a pack code we treat as
    unknown and let the year rule resolve.
    """
    out = {}
    vins = [v for v in vins if v]
    for i in range(0, len(vins), 50):           # batch endpoint takes 50 at a time
        chunk = vins[i:i + 50]
        data = ";".join(chunk)                   # "VIN1;VIN2;..." per NHTSA batch format
        for attempt in range(3):
            try:
                resp = requests.post(
                    VPIC_BATCH_URL,
                    data={"format": "json", "DATA": data},
                    timeout=40,
                )
                resp.raise_for_status()
                for r in resp.json().get("Results", []):
                    info = (r.get("BatteryInfo") or "").lower()
                    vin = r.get("VIN")
                    if not vin:
                        continue
                    if "extended range" in info:
                        out[vin] = "yes"
                    elif "standard range" in info:
                        out[vin] = "no"
                    else:
                        out[vin] = "unknown"
                break
            except requests.RequestException:
                if attempt == 2:
                    print(f"  (VIN decode failed for a batch of {len(chunk)})")
                else:
                    time.sleep(2)
    return out


def heading_signal(listing):
    """Occasionally a listing spells out the battery in its title."""
    text = (listing.get("heading") or "").lower()
    if any(s in text for s in ("extended range", "ext range", "ext. range", "131 kwh")):
        return "yes"
    if any(s in text for s in ("standard range", "std range", "98 kwh")):
        return "no"
    return "unknown"


def er_signal(listing, battery):
    """Extended Range verdict: heading text first, then the VIN decode."""
    sig = heading_signal(listing)
    if sig != "unknown":
        return sig
    return battery.get(listing_id(listing), "unknown")


def bad_title(listing):
    """True if the listing text openly flags a salvage/branded/damaged title."""
    text = " ".join([
        listing.get("heading") or "",
        listing.get("seller_comments") or "",
        listing.get("description") or "",
        listing.get("dealer_comments") or "",
    ]).lower()
    return any(w in text for w in BAD_TITLE_WORDS)


def price_ok(listing):
    """Per-year price gate: 2025 up to its higher cap, everything else $52k."""
    price = listing.get("price")
    if not price:
        return True  # unknown price -> let it through rather than silently drop
    cap = PRICE_MAX_BY_YEAR.get(get_year(listing), PRICE_MAX)
    return price <= cap


def fresh_enough(listing):
    """Drop listings MarketCheck hasn't re-seen within LAST_SEEN_MAX_DAYS.
    When a truck sells, the source stops showing it and last_seen_at goes
    stale, so this removes the clearly-gone ones. Unknown timestamp -> keep."""
    ts = listing.get("last_seen_at")
    if not ts:
        return True
    try:
        return (time.time() - float(ts)) <= LAST_SEEN_MAX_DAYS * 86400
    except (TypeError, ValueError):
        return True


def evaluate(listing, battery):
    """(include, note) per Ash's criteria. note='' means a clean match."""
    trim = get_trim(listing)
    if trim not in TARGET_TRIMS:
        return (False, "")

    if not fresh_enough(listing):   # stale / likely-sold -> drop
        return (False, "")

    if not price_ok(listing):       # over the cap for its model year
        return (False, "")

    if bad_title(listing):          # salvage/branded/damaged title in the text
        return (False, "")

    year = get_year(listing)
    sig = er_signal(listing, battery)

    if trim == "platinum":          # always Extended Range
        return (True, "")

    if trim == "flash":             # ER standard; PPO level not in the data
        if sig == "no":
            return (False, "")
        return (True, "verify 9.6kW Pro Power Onboard")

    if trim == "lariat":
        # Battery first.
        if sig == "no":             # confirmed Standard Range -> drop
            return (False, "")
        er_ok = sig == "yes" or (year and year >= 2024)  # VIN/text, or ER-by-year
        if not er_ok:
            return (True, "battery unconfirmed - verify Extended Range")
        # ER is good. 2025 dropped 9.6kW PPO from standard, so flag it.
        if year == 2025:
            return (True, "verify Pro Power Onboard (not standard on 2025)")
        return (True, "")

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
YEAR_NOTES = {
    2022: "most content · best price",
    2023: "watch electrical complaints",
    2024: "balanced pick",
    2025: "newest · best battery warranty",
}


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
    ynote = YEAR_NOTES.get(year if isinstance(year, int) else get_year(listing), "")
    ynote_html = f"<br><span style='color:#888;font-size:12px;'>{ynote}</span>" if ynote else ""
    return (
        f"<tr><td style='{cell}'>{year} Lightning {trim}{ynote_html}</td>"
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
        f"{YEARS[0]}-{YEARS[-1]}, under ${PRICE_MAX:,} (2025s up to "
        f"${PRICE_MAX_BY_YEAR.get(2025, PRICE_MAX):,}) and {MILES_MAX:,} miles, "
        f"nationwide. Salvage/branded titles that say so are filtered out. "
        f"Anything in the Verify column needs a quick look on the listing "
        f"before you get excited.</p>"
        "<table style='border-collapse:collapse;width:100%;'>"
        f"<tr><th style='{head}'>Vehicle</th><th style='{head}'>Price</th>"
        f"<th style='{head}'>Miles</th><th style='{head}'>Location</th>"
        f"<th style='{head}'>Source</th><th style='{head}'>Verify</th>"
        f"<th style='{head}'>Link</th></tr>"
        f"{rows}</table></body></html>"
    )


MATCHES_FILE = Path("matches.json")


def write_matches_json(matches):
    """Save the full current match pool as data the dashboard reads."""
    items = []
    for l in sorted(matches, key=lambda x: x.get("price") or 1e12):
        items.append({
            "year": get_year(l),
            "trim": (get_trim(l) or "").title(),
            "price": l.get("price"),
            "miles": l.get("miles"),
            "location": get_location(l),
            "source": "private" if l.get("_source") == "private" else "dealer",
            "note": l.get("_note", ""),
            "url": get_url(l),
            "vin": listing_id(l),
            "last_seen": l.get("last_seen_at"),          # Unix seconds, for freshness
            "dom": l.get("dom_active") or l.get("dom"),   # active days on market
        })
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(items),
        "criteria": {
            "years": YEARS,
            "price_max": PRICE_MAX,
            "price_max_2025": PRICE_MAX_BY_YEAR.get(2025),
            "miles_max": MILES_MAX,
            "trims": sorted(TARGET_TRIMS),
        },
        "items": items,
    }
    MATCHES_FILE.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {MATCHES_FILE} ({len(items)} items)")


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

    # Only the right trims are candidates; decode their VINs to confirm battery.
    candidates = [l for l in listings if get_trim(l) in TARGET_TRIMS]
    lariat_vins = [listing_id(l) for l in candidates if get_trim(l) == "lariat"]
    print(f"Candidates (right trims): {len(candidates)}; decoding {len(lariat_vins)} Lariat VINs...")
    battery = decode_batteries(lariat_vins)
    er_yes = sum(1 for v in battery.values() if v == "yes")
    er_no = sum(1 for v in battery.values() if v == "no")
    print(f"  VIN decode: {er_yes} Extended Range, {er_no} Standard Range, "
          f"{len(battery) - er_yes - er_no} unresolved")
    title_flags = sum(1 for l in candidates if bad_title(l))
    print(f"  Title scan: {title_flags} candidate(s) dropped for salvage/branded text")

    if INSPECT and candidates:
        print("\nWhat the filter sees on the first 15 candidates:")
        for l in candidates[:15]:
            inc, note = evaluate(l, battery)
            print(f"  {get_year(l)} {get_trim(l):8} ER={er_signal(l, battery):7} "
                  f"keep={inc} {note} | {(l.get('heading') or '')[:55]}")

    matches = []
    for l in candidates:
        include, note = evaluate(l, battery)
        if include:
            l["_note"] = note
            matches.append(l)
    clean = sum(1 for l in matches if not l["_note"])
    print(f"Target matches: {len(matches)} ({clean} clean, {len(matches) - clean} to verify)")

    write_matches_json(matches)   # full current pool -> data for the dashboard

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
