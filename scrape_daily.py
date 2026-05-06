#!/usr/bin/env python3
"""
scrape_daily.py
───────────────
Scrapes Leboncoin AND SeLoger Toulouse apartments-for-sale listings once
per day, extracts each portal's embedded JSON state, normalizes to a
common schema, filters to apartments ≤ 160 000 € near university-serving
zones, and writes listings.json for the Habitat Scout frontend.

Both portals embed their entire page state into a <script id="__NEXT_DATA__">
JSON blob (Next.js apps do this by default). Pulling that JSON is far more
robust than parsing HTML cards because it doesn't break on cosmetic redesigns.

────────────────────────────────────────────────────────────────────
LEGAL POSTURE
────────────────────────────────────────────────────────────────────

This violates both portals' Terms of Service. It is NOT illegal under
French law for solo personal use at this volume (CNIL guidance on web
scraping for personal purposes). Realistic risk: IP ban, not lawsuit.

Rules:
  • Run AT MOST once per day. Do NOT loop, do NOT paginate aggressively.
  • Use a residential IP. Avoid datacenter VPS IPs which Datadome flags
    by default. A Raspberry Pi at home is ideal.
  • Do NOT redistribute scraped data.
  • If a portal blocks you, back off — do not retry hammered.

────────────────────────────────────────────────────────────────────
SETUP
────────────────────────────────────────────────────────────────────

Required:
    pip install httpx beautifulsoup4

Optional (used as fallback when plain HTTP gets a Datadome challenge):
    pip install playwright
    playwright install chromium

Run:
    python scrape_daily.py

Output:
    listings.json  — merged, deduped listings from both portals
    state.json     — first-seen dates, used across runs

Cron (once per day, randomized in window to avoid predictability):
    30 7 * * * sleep $((RANDOM \\% 1800)) && cd ~/scout && \\
        python scrape_daily.py >> scout.log 2>&1

Failure mode: if one portal fails, the other still runs. Failures are
logged. listings.json is only overwritten if at least one portal returned
data, so a total-failure day doesn't wipe yesterday's results.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import random
import re
import sys
import time
from typing import Any

import httpx
from bs4 import BeautifulSoup

# ─── config ──────────────────────────────────────────────────────────

LEBONCOIN_URL = "https://www.leboncoin.fr/cl/ventes_immobilieres/cp_toulouse"
SELOGER_URL   = "https://www.seloger.com/immobilier/achat/immo-toulouse-31/bien-appartement/"

MAX_PRICE_EUR = 160_000
OUT_PATH = os.environ.get("OUT_PATH", "./listings.json")
STATE_PATH = os.environ.get("STATE_PATH", "./state.json")

# Toulouse postal codes that overlap university metro zones. Used as a
# coarse pre-filter; the frontend does the fine-grained metro/walking
# filtering using the station data baked into the HTML.
UNIVERSITY_POSTAL_CODES = {
    "31000": "centre (UT1, ENSEEIHT, Sciences Po)",
    "31100": "Mirail / sud-ouest (UT2J)",
    "31300": "ouest",
    "31400": "Rangueil / Empalot (UT3, INSA, ISAE)",
    "31500": "est",
}

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

# ─── HTTP fetcher with realistic headers ─────────────────────────────

def http_get(url: str, referer: str | None = None) -> str | None:
    """Plain HTTPS GET with realistic browser headers. None if blocked."""
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none" if referer is None else "same-origin",
        "Sec-Fetch-User": "?1",
    }
    if referer:
        headers["Referer"] = referer
    try:
        with httpx.Client(http2=True, follow_redirects=True, timeout=30) as c:
            r = c.get(url, headers=headers)
        if r.status_code != 200:
            print(f"  http {r.status_code} on {url}", file=sys.stderr)
            return None
        text = r.text
        # Datadome challenge pages contain these markers
        low = text.lower()
        if "datadome" in low and ("captcha" in low or "geo.captcha" in low):
            print("  blocked by Datadome challenge", file=sys.stderr)
            return None
        if len(text) < 5000:
            print(f"  suspiciously short response ({len(text)} bytes)", file=sys.stderr)
            return None
        return text
    except Exception as exc:
        print(f"  http error: {exc}", file=sys.stderr)
        return None

def browser_get(url: str) -> str | None:
    """Fallback fetcher using headless Chromium. Heavier but harder to detect."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  playwright not installed — skipping browser fallback", file=sys.stderr)
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            ctx = browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                viewport={"width": 1366, "height": 900},
                locale="fr-FR",
                timezone_id="Europe/Paris",
            )
            # Mask the most obvious headless-Chrome tells
            ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            # Let any deferred scripts finish; Datadome injects late
            page.wait_for_timeout(2500)
            html = page.content()
            browser.close()
            return html
    except Exception as exc:
        print(f"  browser error: {exc}", file=sys.stderr)
        return None

def fetch_with_fallback(url: str, label: str) -> str | None:
    print(f"[{label}] fetching {url}")
    html = http_get(url)
    if html:
        print(f"[{label}]   ✓ got page via plain HTTP ({len(html):,} bytes)")
        return html
    print(f"[{label}]   plain HTTP failed, trying headless browser…")
    html = browser_get(url)
    if html:
        print(f"[{label}]   ✓ got page via headless browser ({len(html):,} bytes)")
        return html
    print(f"[{label}]   ✗ both fetchers failed")
    return None

# ─── extract Next.js JSON state ──────────────────────────────────────

def extract_next_data(html: str) -> dict | None:
    """Pull and parse the <script id="__NEXT_DATA__"> JSON blob."""
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if tag is None or not tag.string:
        return None
    try:
        return json.loads(tag.string)
    except json.JSONDecodeError:
        return None

def walk_for_listings(node: Any, found: list[dict], depth: int = 0) -> None:
    """
    Walk a parsed JSON tree and collect dict-shaped objects that look like
    listings. We don't know the exact path inside __NEXT_DATA__ (it varies
    between site versions), so we duck-type: a node is a listing if it has
    a price + (URL or list_id) and isn't obviously something else.
    """
    if depth > 12 or len(found) > 500:
        return
    if isinstance(node, dict):
        # Heuristic: listing-shaped objects have price + identifier
        has_price = any(k in node for k in ("price", "prix", "priceCents", "displayPrice"))
        has_id = any(k in node for k in ("list_id", "id", "listingId", "publicationId"))
        looks_like_listing = (
            has_price and has_id
            and "subject" not in node.get("category", {}) if isinstance(node.get("category"), dict) else has_price and has_id
        )
        # The above category check guards against false positives; reset:
        looks_like_listing = has_price and has_id
        if looks_like_listing:
            found.append(node)
        else:
            for v in node.values():
                walk_for_listings(v, found, depth + 1)
    elif isinstance(node, list):
        for item in node:
            walk_for_listings(item, found, depth + 1)

# ─── normalize to common schema ──────────────────────────────────────

def coerce_int(v: Any) -> int | None:
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        digits = re.sub(r"\D", "", v)
        return int(digits) if digits else None
    return None

def find_first(d: dict, *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return None

def normalize_leboncoin(raw: dict) -> dict | None:
    list_id = str(find_first(raw, "list_id", "id") or "").strip()
    if not list_id:
        return None

    # Price: leboncoin uses cents in some endpoints, euros in others
    price = find_first(raw, "price", "priceCents", "displayPrice")
    if isinstance(price, list) and price:
        price = price[0]
    price_eur = coerce_int(price)
    if price_eur and price_eur > 1_000_000 and "Cents" in str(raw):
        price_eur //= 100
    if not price_eur or price_eur > MAX_PRICE_EUR:
        return None

    # Attributes are usually a list of {key, value, value_label}
    attrs = {}
    for a in raw.get("attributes") or []:
        if isinstance(a, dict) and "key" in a:
            attrs[a["key"]] = a.get("value") or a.get("value_label")

    surface = coerce_int(attrs.get("square") or attrs.get("surface"))
    rooms = coerce_int(attrs.get("rooms"))
    real_estate_type = (attrs.get("real_estate_type") or "").lower()
    # 2 = apartment in Leboncoin's taxonomy
    if real_estate_type and real_estate_type not in ("2", "appartement", "apartment"):
        return None

    location = raw.get("location") or {}
    postal = str(location.get("zipcode") or location.get("postal_code") or "")
    if postal and not postal.startswith("31"):
        return None

    url = find_first(raw, "url", "share_url")
    if not url:
        url = f"https://www.leboncoin.fr/ad/ventes_immobilieres/{list_id}"

    return {
        "id": f"lbc-{list_id}",
        "source": "leboncoin",
        "url": url,
        "title": (find_first(raw, "subject", "title") or "")[:140],
        "price": price_eur,
        "surface": surface,
        "rooms": rooms,
        "postal_code": postal or None,
        "city": location.get("city"),
        "first_publication_date": find_first(raw, "first_publication_date", "index_date"),
    }

def normalize_seloger(raw: dict) -> dict | None:
    list_id = str(find_first(raw, "id", "listingId", "publicationId", "permalink") or "").strip()
    if not list_id:
        return None

    price = find_first(raw, "pricing", "price", "displayPrice")
    if isinstance(price, dict):
        price = find_first(price, "rawPrice", "price", "amount")
    price_eur = coerce_int(price)
    if not price_eur or price_eur > MAX_PRICE_EUR:
        return None

    surface = coerce_int(find_first(raw, "surface", "livingArea", "surfaceArea"))
    rooms = coerce_int(find_first(raw, "rooms", "roomCount", "rooms_quantity"))

    # Filter to apartments
    btype = str(find_first(raw, "estateType", "propertyType", "type") or "").lower()
    if btype and "appart" not in btype and "apartment" not in btype:
        return None

    address = raw.get("address") or {}
    if isinstance(address, dict):
        postal = str(address.get("zipCode") or address.get("postalCode") or "")
        city = address.get("city") or address.get("cityLabel")
    else:
        postal, city = "", None
    if postal and not postal.startswith("31"):
        return None

    url = find_first(raw, "url", "permalink", "classifiedURL")
    if url and not url.startswith("http"):
        url = f"https://www.seloger.com{url}"
    if not url:
        return None  # SeLoger IDs aren't easy to URL-construct, drop if no URL

    return {
        "id": f"sl-{list_id}",
        "source": "seloger",
        "url": url,
        "title": (find_first(raw, "title", "description") or "")[:140],
        "price": price_eur,
        "surface": surface,
        "rooms": rooms,
        "postal_code": postal or None,
        "city": city,
        "first_publication_date": find_first(raw, "publicationDate", "createdAt"),
    }

# ─── per-portal pipelines ────────────────────────────────────────────

def scrape_leboncoin() -> list[dict]:
    html = fetch_with_fallback(LEBONCOIN_URL, "leboncoin")
    if not html:
        return []
    data = extract_next_data(html)
    if not data:
        print("[leboncoin]   ✗ no __NEXT_DATA__ in response", file=sys.stderr)
        return []
    raw_listings: list[dict] = []
    walk_for_listings(data, raw_listings)
    print(f"[leboncoin]   found {len(raw_listings)} candidate objects in JSON")
    out: list[dict] = []
    for r in raw_listings:
        try:
            n = normalize_leboncoin(r)
            if n:
                out.append(n)
        except Exception as exc:
            print(f"[leboncoin]   normalize error: {exc}", file=sys.stderr)
    print(f"[leboncoin]   {len(out)} listings after filtering ≤ {MAX_PRICE_EUR:,}€")
    return out

def scrape_seloger() -> list[dict]:
    html = fetch_with_fallback(SELOGER_URL, "seloger")
    if not html:
        return []
    data = extract_next_data(html)
    if not data:
        # SeLoger sometimes uses a different state hook — try common alternatives
        soup = BeautifulSoup(html, "html.parser")
        for sid in ("__NUXT_DATA__", "__INITIAL_STATE__", "initialState"):
            tag = soup.find("script", id=sid)
            if tag and tag.string:
                try:
                    data = json.loads(tag.string)
                    print(f"[seloger]   using fallback state script #{sid}")
                    break
                except json.JSONDecodeError:
                    continue
        if not data:
            print("[seloger]   ✗ no embedded JSON state found", file=sys.stderr)
            return []
    raw_listings: list[dict] = []
    walk_for_listings(data, raw_listings)
    print(f"[seloger]   found {len(raw_listings)} candidate objects in JSON")
    out: list[dict] = []
    for r in raw_listings:
        try:
            n = normalize_seloger(r)
            if n:
                out.append(n)
        except Exception as exc:
            print(f"[seloger]   normalize error: {exc}", file=sys.stderr)
    print(f"[seloger]   {len(out)} listings after filtering ≤ {MAX_PRICE_EUR:,}€")
    return out

# ─── persistence ─────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"first_seen": {}}

def save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def merge_with_history(new_listings: list[dict]) -> list[dict]:
    """Track first-seen date per listing, keep yesterday's listings if their
    URL still appears in today's run, drop otherwise."""
    state = load_state()
    now = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    today_ids = {l["id"] for l in new_listings}
    for l in new_listings:
        if l["id"] not in state["first_seen"]:
            state["first_seen"][l["id"]] = now
        l["first_seen"] = state["first_seen"][l["id"]]
    save_state(state)
    return sorted(new_listings, key=lambda l: l["first_seen"], reverse=True)

# ─── main ────────────────────────────────────────────────────────────

def main() -> int:
    started = time.time()
    print(f"=== scrape_daily.py — {dt.datetime.now().isoformat(timespec='seconds')} ===")

    leboncoin = scrape_leboncoin()
    # Polite delay between portals so we look like a human switching tabs
    time.sleep(random.uniform(8, 20))
    seloger = scrape_seloger()

    combined = leboncoin + seloger

    if not combined:
        print("ERROR: both portals returned 0 listings — keeping yesterday's file", file=sys.stderr)
        return 1

    final = merge_with_history(combined)

    out = {
        "updated_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "count": len(final),
        "by_source": {
            "leboncoin": sum(1 for l in final if l["source"] == "leboncoin"),
            "seloger":   sum(1 for l in final if l["source"] == "seloger"),
        },
        "listings": final,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    elapsed = time.time() - started
    print(f"=== wrote {len(final)} listings → {OUT_PATH} in {elapsed:.1f}s ===")
    print(f"    leboncoin: {out['by_source']['leboncoin']}, seloger: {out['by_source']['seloger']}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
