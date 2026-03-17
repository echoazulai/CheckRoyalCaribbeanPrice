#!/usr/bin/env python3
"""
Celebrity Cruises UK Price Tracker
===================================
Tracks cabin/suite prices for Celebrity Cruises sailings, storing full price
history in a local SQLite database.

SETUP (run once):
    pip install requests pyyaml

USAGE:
    python celebrity_tracker.py                         # Discover sailings + check prices
    python celebrity_tracker.py --summary               # Show summary without fetching
    python celebrity_tracker.py --history "Apex 2026-11-15"
    python celebrity_tracker.py --currency USD --min-nights 10

FILES CREATED:
    celebrity_tracker.db   — SQLite database (all price history)
    tracker_config.yaml    — Edit this to change settings
"""

import requests
import yaml
import sqlite3
import json
import time
import re
import argparse
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# API constants — these mirror the patterns in BrowseRoyalCaribbeanPrice.py
# ---------------------------------------------------------------------------

MOBILE_APP_KEY = "cdCNc04srNq4rBvKofw1aC50dsdSaPuc"
SHIPS_API_URL = "https://api.rccl.com/en/all/mobile/v2/ships"
# Voyages: try celebrity-specific path first, fall back to royal
VOYAGES_URLS = [
    "https://api.rccl.com/en/celebrity/mobile/v3/ships/{ship_code}/voyages",
    "https://api.rccl.com/en/royal/mobile/v3/ships/{ship_code}/voyages",
]
CELEBRITY_GRAPH_URL = "https://www.celebritycruises.com/cruises/graph"

# Celebrity Retreat suite class names / codes  (case-insensitive match)
RETREAT_KEYWORDS = ["suite", "villa", "penthouse", "iconic", "retreat"]
RETREAT_CODES = {"RS", "CS", "SS", "SY", "PH", "IC", "EV", "RF", "A2",
                 "S1", "S2", "S3", "SX", "SK"}

# Guarantee fare keywords / codes
GTY_KEYWORDS = ["guarantee", "gty", "guaranteed"]
GTY_CODES = {"GS", "GT", "GI", "GB", "SG"}

# GraphQL query — same structure the existing repo uses for Royal Caribbean
CRUISE_SEARCH_QUERY = (
    "query cruiseSearch_Cruises($filters: String) {"
    "  cruiseSearch(filters: $filters) {"
    "    results {"
    "      cruises {"
    "        id"
    "        sailings {"
    "          sailDate"
    "          stateroomClassPricing {"
    "            price { value currency { code } }"
    "            stateroomClass { id name content { code } }"
    "          }"
    "        }"
    "      }"
    "    }"
    "  }"
    "}"
)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS sailings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ship_code        TEXT NOT NULL,
    ship_name        TEXT NOT NULL,
    sail_date        TEXT NOT NULL,
    duration_nights  INTEGER,
    itinerary_name   TEXT,
    package_code     TEXT,
    group_id         TEXT,
    first_seen       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ship_code, sail_date)
);

CREATE TABLE IF NOT EXISTS price_checks (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    sailing_id           INTEGER NOT NULL,
    checked_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    cabin_class          TEXT NOT NULL,
    cabin_category       TEXT,
    category_code        TEXT,
    is_guarantee         INTEGER DEFAULT 0,
    is_retreat           INTEGER DEFAULT 0,
    price_per_person_gbp REAL,
    price_total_gbp      REAL,
    taxes_and_fees_gbp   REAL,
    available            INTEGER DEFAULT 1,
    currency             TEXT DEFAULT 'GBP',
    raw_response         TEXT,
    FOREIGN KEY (sailing_id) REFERENCES sailings(id)
);

CREATE INDEX IF NOT EXISTS idx_price_checks_sailing
    ON price_checks(sailing_id, checked_at);
CREATE INDEX IF NOT EXISTS idx_price_checks_retreat
    ON price_checks(is_retreat, available, price_per_person_gbp);
"""


def setup_database(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(DB_SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULTS = {
    "currency": "GBP",
    "country": "GB",
    "passengers": 2,
    "min_nights": 7,
    "cruise_line": "celebrity",
    "ship_filter": [],
    "max_price_pp": None,
}


def load_config(config_path: str = "tracker_config.yaml") -> dict:
    if not Path(config_path).exists():
        print(f"[INFO] Config not found at '{config_path}'; using defaults.")
        return dict(DEFAULTS)
    with open(config_path, "r") as fh:
        user_cfg = yaml.safe_load(fh) or {}
    return {**DEFAULTS, **user_cfg}


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _mobile_headers() -> dict:
    return {
        "appkey": MOBILE_APP_KEY,
        "accept": "application/json",
        "appversion": "1.54.0",
        "accept-language": "en",
        "user-agent": "okhttp/4.10.0",
    }


def get_celebrity_ships() -> list[dict]:
    """Return list of {code, name} for all Celebrity Cruises ships."""
    try:
        resp = requests.get(
            SHIPS_API_URL,
            params={"sort": "name"},
            headers=_mobile_headers(),
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as exc:
        print(f"[ERROR] Could not fetch ship list: {exc}")
        return []

    ships = []
    for ship in resp.json().get("payload", {}).get("ships", []):
        name = str(ship.get("name", ""))
        brand = str(ship.get("brand", "")).upper()
        # Celebrity ships: name starts with "Celebrity" OR brand code is "C"
        if name.startswith("Celebrity") or brand in ("C", "CELEBRITY"):
            ships.append({"code": ship.get("shipCode"), "name": name})
    return ships


def get_sailings_for_ship(ship_code: str) -> list[dict]:
    """Return all future voyages for a ship.  Each dict has sail_date (ISO),
    description, voyage_code, duration_nights."""
    headers = _mobile_headers()
    for url_tpl in VOYAGES_URLS:
        url = url_tpl.format(ship_code=ship_code)
        try:
            resp = requests.get(url, params={"resultSet": "300"}, headers=headers, timeout=30)
            if resp.status_code != 200:
                continue
            voyages = resp.json().get("payload", {}).get("voyages", [])
            results = []
            for v in voyages:
                raw_date = v.get("sailDate", "")          # YYYYMMDD
                if len(raw_date) == 8:
                    sail_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
                else:
                    sail_date = raw_date

                desc = v.get("voyageDescription", "")
                m = re.search(r"(\d+)[\s\-]*[Nn]ight", desc)
                nights = int(m.group(1)) if m else None

                results.append({
                    "sail_date": sail_date,
                    "description": desc,
                    "voyage_code": v.get("voyageCode"),
                    "voyage_id": v.get("voyageId"),
                    "duration_nights": nights,
                })
            return results
        except Exception as exc:
            print(f"[WARN] Voyages API error ({url}): {exc}")
    return []


def _graph_headers(currency: str, country_code: str) -> dict:
    """Headers for the Celebrity cruise-search GraphQL endpoint."""
    alpha2 = country_code[:2].upper() if country_code else "GB"
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:142.0) Gecko/20100101 Firefox/142.0",
        "Accept": "*/*",
        "Accept-Language": "en-GB,en;q=0.9",
        "content-type": "application/json",
        "brand": "C",
        "country": country_code,
        "language": "en",
        "currency": currency,
        "office": "SOU",
        "countryalpha2code": alpha2,
        "apollographql-client-name": "celebrity-NextGen-Cruise-Search",
        "skip_authentication": "true",
        "request-timeout": "20",
        "Origin": "https://www.celebritycruises.com",
        "Referer": "https://www.celebritycruises.com/",
        "Connection": "keep-alive",
    }


def get_cabin_pricing(
    ship_code: str,
    sail_date: str,      # YYYY-MM-DD
    voyage_code: str | None,
    currency: str = "GBP",
    country: str = "GBR",
    adults: int = 2,
) -> list[dict] | None:
    """Query Celebrity cruise-search GraphQL for stateroom class pricing.

    Returns list of dicts, or None on hard failure (caller should skip sailing).
    Empty list means sailing was found but no cabin pricing returned.
    """
    headers = _graph_headers(currency, country)

    # Build filter strings to try in order
    filters = []
    if voyage_code:
        filters.append(
            f"id:{voyage_code}|adults:{adults}|children:0"
            f"|startDate:{sail_date}~{sail_date}"
        )
    # Broader fallback: search by ship code + date
    filters.append(
        f"shipCode:{ship_code}|adults:{adults}|children:0"
        f"|startDate:{sail_date}~{sail_date}"
    )

    for filter_str in filters:
        payload = {
            "operationName": "cruiseSearch_Cruises",
            "variables": {
                "filters": filter_str,
                "enableNewCasinoExperience": False,
                "sort": {"by": "RECOMMENDED"},
                "pagination": {"count": 100, "skip": 0},
            },
            "query": CRUISE_SEARCH_QUERY,
        }
        try:
            resp = requests.post(
                CELEBRITY_GRAPH_URL, headers=headers, json=payload, timeout=30
            )
            if resp.status_code != 200:
                continue

            data = resp.json().get("data") or {}
            cruises = (
                data.get("cruiseSearch", {})
                    .get("results", {})
                    .get("cruises", [])
            )
            if not cruises:
                continue

            # Find the sailing that matches our date
            target_date_clean = sail_date.replace("-", "")
            for cruise in cruises:
                for sailing in cruise.get("sailings", []):
                    api_date = sailing.get("sailDate", "").replace("-", "")
                    if api_date != target_date_clean:
                        continue

                    pricing_list = sailing.get("stateroomClassPricing", [])
                    results = []
                    for p in pricing_list:
                        sc = p.get("stateroomClass", {})
                        price_info = p.get("price")

                        class_name = sc.get("name", "Unknown")
                        class_code = sc.get("content", {}).get("code", "")

                        price_pp = None
                        price_currency = currency
                        if price_info and price_info.get("value") is not None:
                            try:
                                price_pp = float(price_info["value"])
                            except (TypeError, ValueError):
                                pass
                            price_currency = (
                                price_info.get("currency", {}).get("code") or currency
                            )

                        results.append({
                            "class_name": class_name,
                            "class_code": class_code,
                            "price_per_person": price_pp,
                            "currency": price_currency,
                            "raw": p,
                        })
                    return results

        except Exception as exc:
            print(f"[WARN] Pricing API error for {ship_code} {sail_date}: {exc}")

    # All attempts failed — distinguish "not found" from error
    return None


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

def classify_cabin(class_name: str, class_code: str) -> tuple[bool, bool]:
    """Return (is_retreat, is_guarantee)."""
    name_lc = class_name.lower()
    code_uc = (class_code or "").upper()

    is_retreat = any(kw in name_lc for kw in RETREAT_KEYWORDS) or code_uc in RETREAT_CODES
    is_guarantee = (
        any(kw in name_lc for kw in GTY_KEYWORDS)
        or code_uc in GTY_CODES
        or name_lc.endswith(" gty")
        or "gtd" in name_lc
    )
    return is_retreat, is_guarantee


# ---------------------------------------------------------------------------
# Country code normalisation
# ---------------------------------------------------------------------------

_COUNTRY_MAP = {
    "GB": "GBR", "US": "USA", "AU": "AUS", "CA": "CAN",
    "DE": "DEU", "FR": "FRA", "IE": "IRL",
}


def normalise_country(code: str) -> str:
    return _COUNTRY_MAP.get(code.upper(), code.upper())


# ---------------------------------------------------------------------------
# Core run cycle
# ---------------------------------------------------------------------------

def run_discovery(config: dict, conn: sqlite3.Connection) -> int:
    """Discover Celebrity sailings; insert new ones. Returns count of new rows."""
    min_nights = config.get("min_nights", 7)
    ship_filter = [s.strip().lower() for s in (config.get("ship_filter") or [])]

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{ts}] Discovering Celebrity Cruises sailings...")

    ships = get_celebrity_ships()
    if not ships:
        print("[ERROR] No Celebrity ships returned from API.")
        return 0
    print(f"  Found {len(ships)} Celebrity ships in total")

    if ship_filter:
        ships = [s for s in ships if any(f in s["name"].lower() for f in ship_filter)]
        print(f"  Filtered to {len(ships)} ships matching: {ship_filter}")

    new_count = 0
    cur = conn.cursor()
    today = datetime.now().date()

    for ship in ships:
        ship_code = ship["code"]
        ship_name = ship["name"]

        sailings = get_sailings_for_ship(ship_code)
        time.sleep(0.5)   # be polite to the API

        for s in sailings:
            sail_date = s["sail_date"]

            # Skip past sailings
            try:
                if datetime.strptime(sail_date, "%Y-%m-%d").date() < today:
                    continue
            except ValueError:
                pass

            # Skip if below min_nights
            nights = s["duration_nights"]
            if min_nights and nights and nights < min_nights:
                continue

            try:
                cur.execute(
                    """
                    INSERT OR IGNORE INTO sailings
                        (ship_code, ship_name, sail_date, duration_nights,
                         itinerary_name, package_code)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (ship_code, ship_name, sail_date, nights,
                     s["description"], s["voyage_code"]),
                )
                if cur.rowcount > 0:
                    new_count += 1
                    print(
                        f"  New sailing: {ship_name} {sail_date}"
                        f" ({nights} nights) — {s['description']}"
                    )
            except sqlite3.Error as exc:
                print(f"  [DB ERROR] {exc}")

    conn.commit()
    print(f"  Discovery complete. New sailings added: {new_count}")
    return new_count


def run_price_checks(config: dict, conn: sqlite3.Connection) -> int:
    """Check prices for every known future sailing. Returns total rows inserted."""
    currency = config.get("currency", "GBP")
    country = normalise_country(config.get("country", "GB"))
    adults = config.get("passengers", 2)
    max_price = config.get("max_price_pp")

    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, ship_code, ship_name, sail_date, package_code
        FROM sailings
        WHERE sail_date >= date('now')
        ORDER BY sail_date
        """
    )
    sailings = cur.fetchall()

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{ts}] Checking prices for {len(sailings)} sailings "
          f"(currency={currency}, country={country}, adults={adults})...")

    total_inserted = 0

    for sailing_id, ship_code, ship_name, sail_date, package_code in sailings:

        pricing = get_cabin_pricing(
            ship_code, sail_date, package_code, currency, country, adults
        )
        time.sleep(1.5)  # rate limiting

        if pricing is None:
            print(f"  [SKIP] {ship_name} {sail_date} — API error, skipping")
            continue

        # Determine which classes were seen in the most recent previous check
        cur.execute(
            """
            SELECT DISTINCT cabin_class
            FROM price_checks
            WHERE sailing_id = ?
              AND checked_at = (SELECT MAX(checked_at) FROM price_checks WHERE sailing_id = ?)
            """,
            (sailing_id, sailing_id),
        )
        previously_seen = {row[0] for row in cur.fetchall()}
        current_classes = set()
        rows_this_sailing = 0

        for p in pricing:
            class_name = p["class_name"]
            class_code = p["class_code"]
            price_pp = p["price_per_person"]
            price_cur = p["currency"]

            # Apply max price filter
            if max_price and price_pp and price_pp > max_price:
                current_classes.add(class_name)
                continue

            is_retreat, is_guarantee = classify_cabin(class_name, class_code)
            price_total = (price_pp * adults) if price_pp is not None else None
            available = 1 if price_pp is not None else 0

            cur.execute(
                """
                INSERT INTO price_checks
                    (sailing_id, cabin_class, cabin_category, category_code,
                     is_guarantee, is_retreat,
                     price_per_person_gbp, price_total_gbp,
                     available, currency, raw_response)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sailing_id, class_name, class_name, class_code,
                    1 if is_guarantee else 0,
                    1 if is_retreat else 0,
                    price_pp, price_total,
                    available, price_cur,
                    json.dumps(p["raw"]),
                ),
            )
            current_classes.add(class_name)
            total_inserted += 1
            rows_this_sailing += 1

        # Mark classes no longer in response as unavailable
        for missing in previously_seen - current_classes:
            is_retreat, is_guarantee = classify_cabin(missing, "")
            cur.execute(
                """
                INSERT INTO price_checks
                    (sailing_id, cabin_class, cabin_category,
                     is_guarantee, is_retreat,
                     price_per_person_gbp, price_total_gbp,
                     available, currency)
                VALUES (?, ?, ?, ?, ?, NULL, NULL, 0, ?)
                """,
                (sailing_id, missing, missing,
                 1 if is_guarantee else 0,
                 1 if is_retreat else 0,
                 currency),
            )
            total_inserted += 1

        avail_count = sum(1 for p in pricing if p["price_per_person"] is not None)
        print(
            f"  {ship_name} {sail_date}: "
            f"{len(current_classes)} cabin types checked, {avail_count} with prices"
        )
        conn.commit()

    return total_inserted


# ---------------------------------------------------------------------------
# Summary & history output
# ---------------------------------------------------------------------------

def print_summary(conn: sqlite3.Connection, min_nights: int = 7, new_sailings: int = 0) -> None:
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM sailings WHERE sail_date >= date('now')")
    total_sailings = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM price_checks")
    total_checks = cur.fetchone()[0]

    print("\n" + "=" * 60)
    print("=== Celebrity Cruises UK Price Tracker ===")
    print(f"Run completed : {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"Sailings tracked : {total_sailings} ({new_sailings} new this run)")
    print(f"Price checks recorded : {total_checks:,}")

    # ---- Cheapest Retreat suites ----
    print(f"\n--- Cheapest Retreat Suites ({min_nights}+ nights, per person) ---")
    cur.execute(
        """
        SELECT
            s.ship_name, s.sail_date, s.duration_nights,
            pc.cabin_class, pc.is_guarantee,
            pc.price_per_person_gbp,
            CASE WHEN s.duration_nights > 0
                 THEN CAST(ROUND(pc.price_per_person_gbp / s.duration_nights) AS INTEGER)
                 ELSE NULL END AS price_per_night
        FROM price_checks pc
        JOIN sailings s ON pc.sailing_id = s.id
        WHERE pc.is_retreat = 1
          AND pc.available = 1
          AND pc.price_per_person_gbp IS NOT NULL
          AND s.sail_date >= date('now')
          AND (s.duration_nights IS NULL OR s.duration_nights >= ?)
          AND pc.checked_at = (
              SELECT MAX(pc2.checked_at)
              FROM price_checks pc2
              WHERE pc2.sailing_id = pc.sailing_id
                AND pc2.cabin_class = pc.cabin_class
          )
        ORDER BY pc.price_per_person_gbp
        LIMIT 10
        """,
        (min_nights,),
    )
    rows = cur.fetchall()
    if rows:
        for i, (ship, date, nights, cabin, gty, pp, ppn) in enumerate(rows, 1):
            gty_tag = " GTY" if gty and "gty" not in cabin.lower() else ""
            nights_str = f"{nights} nights" if nights else "? nights"
            ppn_str = f" | £{ppn:,}/night" if ppn else ""
            print(f"  {i:2}. {ship} | {date} | {nights_str} | {cabin}{gty_tag} | £{pp:,.0f} pp{ppn_str}")
    else:
        print("  No Retreat suite data yet. Run without --summary first.")

    # ---- Biggest price drops ----
    print("\n--- Biggest Price Drops Since Last Run ---")
    cur.execute(
        """
        WITH ranked AS (
            SELECT
                sailing_id,
                cabin_class,
                price_per_person_gbp,
                checked_at,
                ROW_NUMBER() OVER (
                    PARTITION BY sailing_id, cabin_class
                    ORDER BY checked_at DESC
                ) AS rn
            FROM price_checks
            WHERE price_per_person_gbp IS NOT NULL
        ),
        pairs AS (
            SELECT
                sailing_id, cabin_class,
                MAX(CASE WHEN rn = 1 THEN price_per_person_gbp END) AS curr,
                MAX(CASE WHEN rn = 2 THEN price_per_person_gbp END) AS prev
            FROM ranked
            WHERE rn <= 2
            GROUP BY sailing_id, cabin_class
        )
        SELECT
            s.ship_name, s.sail_date, p.cabin_class,
            p.prev, p.curr,
            (p.prev - p.curr) AS drop_amt,
            ROUND((p.prev - p.curr) / p.prev * 100.0, 1) AS drop_pct
        FROM pairs p
        JOIN sailings s ON p.sailing_id = s.id
        WHERE p.prev IS NOT NULL
          AND p.curr IS NOT NULL
          AND p.prev > p.curr
          AND s.sail_date >= date('now')
        ORDER BY drop_amt DESC
        LIMIT 10
        """
    )
    drops = cur.fetchall()
    if drops:
        for i, (ship, date, cabin, prev, curr, drop, pct) in enumerate(drops, 1):
            print(
                f"  {i:2}. {ship} | {date} | {cabin}"
                f" | was £{prev:,.0f} → now £{curr:,.0f} (−£{drop:,.0f}, −{pct}%)"
            )
    else:
        print("  No price drop data yet (requires at least 2 runs).")

    print("=" * 60)


def show_history(conn: sqlite3.Connection, query: str) -> None:
    """Show price history for a specific sailing (e.g. 'Apex 2026-11-15')."""
    parts = query.rsplit(" ", 1)
    if len(parts) != 2:
        print("[ERROR] Use format: 'ShipName YYYY-MM-DD'  (e.g. 'Apex 2026-11-15')")
        return
    ship_part, date_str = parts

    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, ship_name, sail_date, duration_nights
        FROM sailings
        WHERE LOWER(ship_name) LIKE ? AND sail_date = ?
        """,
        (f"%{ship_part.lower()}%", date_str),
    )
    row = cur.fetchone()
    if not row:
        print(f"[ERROR] No sailing found matching '{query}'")
        return

    sailing_id, ship_name, sail_date, nights = row
    print(f"\n=== Price History: {ship_name} {sail_date}"
          f" ({nights} nights) ===")

    cur.execute(
        """
        SELECT checked_at, cabin_class, price_per_person_gbp, available,
               is_retreat, is_guarantee, currency
        FROM price_checks
        WHERE sailing_id = ?
        ORDER BY cabin_class, checked_at
        """,
        (sailing_id,),
    )
    rows = cur.fetchall()
    current_class = None
    for (checked_at, cabin_class, pp, avail,
         is_retreat, is_gty, cur_code) in rows:
        if cabin_class != current_class:
            tags = []
            if is_retreat:
                tags.append("RETREAT")
            if is_gty:
                tags.append("GTY")
            tag_str = f"  [{', '.join(tags)}]" if tags else ""
            print(f"\n  {cabin_class}{tag_str}:")
            current_class = cabin_class
        if avail and pp is not None:
            print(f"    {checked_at}  £{pp:,.0f} pp  ({cur_code})")
        else:
            print(f"    {checked_at}  NOT AVAILABLE")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Celebrity Cruises UK Price Tracker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python celebrity_tracker.py\n"
            "  python celebrity_tracker.py --summary\n"
            "  python celebrity_tracker.py --history \"Apex 2026-11-15\"\n"
            "  python celebrity_tracker.py --currency USD --min-nights 10\n"
        ),
    )
    parser.add_argument("--config", default="tracker_config.yaml",
                        help="Path to config YAML (default: tracker_config.yaml)")
    parser.add_argument("--db", default="celebrity_tracker.db",
                        help="Path to SQLite database (default: celebrity_tracker.db)")
    parser.add_argument("--summary", action="store_true",
                        help="Print summary from stored data without fetching new prices")
    parser.add_argument("--history", metavar="QUERY",
                        help="Show price history (e.g. --history \"Apex 2026-11-15\")")
    parser.add_argument("--currency", help="Override currency code (e.g. USD)")
    parser.add_argument("--country", help="Override country code (e.g. US)")
    parser.add_argument("--min-nights", type=int,
                        help="Override minimum nights filter")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.currency:
        config["currency"] = args.currency.upper()
    if args.country:
        config["country"] = args.country.upper()
    if args.min_nights:
        config["min_nights"] = args.min_nights

    conn = setup_database(args.db)

    try:
        if args.history:
            show_history(conn, args.history)
            return

        if args.summary:
            print_summary(conn, config.get("min_nights", 7))
            return

        # --- Full run ---
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{ts}] Celebrity Cruises UK Price Tracker starting")
        print(f"  Config : currency={config['currency']}, "
              f"country={config['country']}, "
              f"min_nights={config['min_nights']}, "
              f"passengers={config['passengers']}")

        new_sailings = run_discovery(config, conn)
        total_checks = run_price_checks(config, conn)
        print_summary(conn, config.get("min_nights", 7), new_sailings)

        ts2 = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{ts2}] Finished. New sailings: {new_sailings},"
              f" price-check rows inserted: {total_checks}\n")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
