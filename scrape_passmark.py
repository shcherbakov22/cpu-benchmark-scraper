#!/usr/bin/env python3
"""Scrape PassMark CPU benchmarks.

PassMark has clean HTML with ~5,925 CPUs on a single page.
No anti-bot measures — simple HTTP requests work.

Usage:
    python3 scrape_passmark.py              # scrape all CPUs
    python3 scrape_passmark.py --limit 100  # first 100 only
"""
import argparse
import os
import re
import sqlite3
import time
from bs4 import BeautifulSoup

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "benchmarks.sqlite")

PASSMARK_URL = "https://www.cpubenchmark.net/cpu-list/all"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}


def init_schema(conn):
    """Create passmark table if not exists."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS passmark (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            cpu_id          INTEGER NOT NULL REFERENCES cpus(id) ON DELETE CASCADE,
            cpu_name        TEXT NOT NULL,
            passmark_id     INTEGER,
            cpu_mark        INTEGER,
            rank            INTEGER,
            price_usd       REAL,
            scraped_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_passmark_cpu_id ON passmark (cpu_id);
        CREATE INDEX IF NOT EXISTS idx_passmark_passmark_id ON passmark (passmark_id);
        CREATE INDEX IF NOT EXISTS idx_passmark_cpu_name ON passmark (cpu_name);
    """)
    conn.commit()


def fetch_page():
    """Fetch the PassMark CPU list page."""
    import requests
    print(f"Fetching {PASSMARK_URL} ...")
    resp = requests.get(PASSMARK_URL, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    print(f"  Got {len(resp.text)} bytes")
    return resp.text


def parse_cpu_list(html):
    """Parse the PassMark CPU list table.

    Returns list of dicts with: name, passmark_id, cpu_mark, rank, price
    """
    soup = BeautifulSoup(html, 'html.parser')
    rows = soup.select('#cputable tbody tr[id^="cpu"]')
    print(f"  Found {len(rows)} CPU rows")

    cpus = []
    for row in rows:
        cpu_id_match = re.search(r'cpu(\d+)', row.get('id', ''))
        if not cpu_id_match:
            continue

        cells = row.select('td')
        if len(cells) < 3:
            continue

        # CPU name from link
        name_link = cells[0].select_one('a')
        if not name_link:
            continue
        cpu_name = name_link.get_text(strip=True)

        # PassMark ID from link params or row id
        passmark_id = int(cpu_id_match.group(1))

        # CPU Mark (second column)
        mark_text = cells[1].get_text(strip=True).replace(',', '')
        try:
            cpu_mark = int(mark_text)
        except ValueError:
            cpu_mark = None

        # Rank (third column)
        rank_text = cells[2].get_text(strip=True)
        try:
            rank = int(rank_text)
        except ValueError:
            rank = None

        # Price (fifth column, if available)
        price = None
        if len(cells) >= 5:
            price_text = cells[4].get_text(strip=True)
            price_match = re.search(r'\$([\d,]+)', price_text)
            if price_match:
                try:
                    price = float(price_match.group(1).replace(',', ''))
                except ValueError:
                    pass

        cpus.append({
            'name': cpu_name,
            'passmark_id': passmark_id,
            'cpu_mark': cpu_mark,
            'rank': rank,
            'price': price,
        })

    return cpus


def match_cpu(conn, name):
    """Match a PassMark CPU name to our cpus table.

    Returns (cpu_id, matched_name) or (None, None).
    """
    name_clean = name.strip()

    # Exact match first
    row = conn.execute(
        "SELECT id, name FROM cpus WHERE name = ? LIMIT 1",
        (name_clean,)
    ).fetchone()
    if row:
        return row[0], row[1]

    # Strip clock speed and core count for fuzzy match
    clean = re.sub(r'\s*@\s*[\d.]+[GTM]Hz.*', '', name_clean)
    clean = re.sub(r'\s*\(\d+\s*cores?.*', '', clean)
    clean = re.sub(r'\s*\(\d+\s*threads?.*', '', clean)
    clean = clean.strip()

    # Substring match
    row = conn.execute(
        "SELECT id, name FROM cpus WHERE name LIKE ? LIMIT 1",
        (f"%{clean}%",)
    ).fetchone()
    if row:
        return row[0], row[1]

    # Try without "Intel " or "AMD " prefix
    clean2 = re.sub(r'^(Intel|AMD|Apple|Qualcomm)\s+', '', clean)
    row = conn.execute(
        "SELECT id, name FROM cpus WHERE name LIKE ? LIMIT 1",
        (f"%{clean2}%",)
    ).fetchone()
    if row:
        return row[0], row[1]

    return None, None


def main():
    parser = argparse.ArgumentParser(description="Scrape PassMark CPU benchmarks")
    parser.add_argument("--limit", type=int, help="Limit to first N CPUs")
    parser.add_argument("--no-fetch", action="store_true",
                        help="Skip fetching (use existing data)")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    init_schema(conn)

    # Count existing entries
    existing = conn.execute("SELECT COUNT(*) FROM passmark").fetchone()[0]
    print(f"Existing PassMark entries: {existing}")

    # Fetch page
    html = fetch_page()
    cpus = parse_cpu_list(html)

    if args.limit:
        cpus = cpus[:args.limit]

    print(f"\nProcessing {len(cpus)} CPUs ...")
    print()

    matched = 0
    unmatched = 0
    inserted = 0

    for i, cpu in enumerate(cpus, 1):
        cpu_id, matched_name = match_cpu(conn, cpu['name'])

        if cpu_id:
            # Check if already inserted
            existing = conn.execute(
                "SELECT id FROM passmark WHERE cpu_id = ? AND passmark_id = ?",
                (cpu_id, cpu['passmark_id'])
            ).fetchone()

            if not existing:
                conn.execute("""
                    INSERT INTO passmark (cpu_id, cpu_name, passmark_id, cpu_mark, rank, price_usd)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (cpu_id, matched_name, cpu['passmark_id'],
                      cpu['cpu_mark'], cpu['rank'], cpu['price']))
                inserted += 1

            matched += 1
        else:
            unmatched += 1
            if unmatched <= 10:
                print(f"  ✗ No match: {cpu['name']} (PM#{cpu['passmark_id']}, Mark={cpu['cpu_mark']})")

        if i % 500 == 0:
            print(f"  [{i}/{len(cpus)}] matched={matched}, unmatched={unmatched}, inserted={inserted}")

    conn.commit()

    print(f"\n{'='*60}")
    print(f"PassMark scrape complete:")
    print(f"  Total CPUs: {len(cpus)}")
    print(f"  Matched: {matched} ({matched/len(cpus)*100:.1f}%)")
    print(f"  Unmatched: {unmatched} ({unmatched/len(cpus)*100:.1f}%)")
    print(f"  New entries inserted: {inserted}")

    conn.close()


if __name__ == "__main__":
    main()
