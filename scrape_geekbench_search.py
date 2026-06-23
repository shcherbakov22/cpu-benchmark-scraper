#!/usr/bin/env python3
"""Scrape Geekbench 6 search results (high/low scores) for each CPU.

Uses nodriver to bypass Cloudflare and parse server-rendered search pages.
Stores results in a separate `geekbench_search` table.

Usage:
    python3 scrape_geekbench_search.py              # scrape all CPUs
    python3 scrape_geekbench_search.py --limit 100  # first 100 only
    python3 scrape_geekbench_search.py --source notebookcheck  # only NB CPUs
"""
import asyncio
import argparse
import os
import re
import sqlite3
import time
from bs4 import BeautifulSoup

import nodriver as uc

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "benchmarks.sqlite")

# URL template for Geekbench search
SEARCH_URL = "https://browser.geekbench.com/v6/cpu/search?q={query}&sort={sort}&dir={direction}"

# Delays (seconds) between requests to avoid rate limiting
DELAY_BETWEEN_QUERIES = 2.5  # within same CPU (4 queries)
DELAY_BETWEEN_CPUS = 3.0     # between different CPUs


def init_schema(conn):
    """Create geekbench_search table if not exists."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS geekbench_search (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            cpu_id          INTEGER NOT NULL REFERENCES cpus(id) ON DELETE CASCADE,
            cpu_name        TEXT NOT NULL,

            -- single-core scores
            single_high     INTEGER,
            single_low      INTEGER,

            -- multi-core scores
            multi_high      INTEGER,
            multi_low       INTEGER,

            -- metadata
            results_count   INTEGER,  -- total search results found
            scraped_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_gb_search_cpu_id ON geekbench_search (cpu_id);
        CREATE INDEX IF NOT EXISTS idx_gb_search_cpu_name ON geekbench_search (cpu_name);
    """)
    conn.commit()


def get_cpu_names(conn, source=None, limit=None):
    """Get CPU names to scrape from the database."""
    query = """
        SELECT c.id, c.name
        FROM cpus c
        WHERE 1=1
    """
    params = []

    if source == "notebookcheck":
        query += " AND c.nb_id IS NOT NULL"
    elif source == "vray":
        query += " AND c.vray_id IS NOT NULL"
    elif source == "geekbench":
        query += " AND c.geekbench_id IS NOT NULL"

    query += " ORDER BY c.id"

    if limit:
        query += " LIMIT ?"
        params.append(limit)

    cursor = conn.execute(query, params)
    return cursor.fetchall()


def sanitize_query(name):
    """Convert CPU name to a Geekbench search query.

    Strategies:
    - Full name: "Intel Core i7-13700K"
    - Short model: "i7-13700K"
    - Model number only: "13700K"
    """
    name = name.strip()

    # Try to extract model number (most reliable for matching)
    # Intel patterns: i3-12100, i5-13600K, i7-13700K, i9-13900K
    # AMD patterns: Ryzen 5 5600X, Ryzen 7 5800X, Threadripper 3960X
    m = re.search(r'(i[3-9]-\d+[A-Z]?|\d{4,5}[A-Z]?X?|Ryzen \d+ \d+[A-Z]?|Threadripper \d+[A-Z]?)', name)
    if m:
        return m.group(1)

    # Fallback: use last 2-3 words of the name
    parts = name.split()
    if len(parts) >= 3:
        return ' '.join(parts[-3:])
    return ' '.join(parts[-2:]) if len(parts) >= 2 else parts[0]


def parse_search_results(html):
    """Parse Geekbench search results HTML.

    Returns:
        dict with first_result scores and total_count
    Skips invalid (0) scores by walking to the next valid result.
    """
    soup = BeautifulSoup(html, 'html.parser')

    # Extract total results count
    total_count = 0
    small = soup.select_one('h2 small')
    if small:
        m = re.search(r'(\d[\d,]*)', small.get_text())
        if m:
            total_count = int(m.group(1).replace(',', ''))

    # Extract scores from first valid result (skip 0 scores)
    rows = soup.select('.list-col-inner')
    for row in rows:
        scores = row.select('.list-col-text-score')
        if len(scores) >= 2:
            single = int(scores[0].get_text(strip=True))
            multi = int(scores[1].get_text(strip=True))
            if single > 0 and multi > 0:
                return {
                    'single': single,
                    'multi': multi,
                    'total_count': total_count,
                }

    return None


async def scrape_cpu(browser, cpu_id, cpu_name, delay=DELAY_BETWEEN_QUERIES):
    """Scrape high/low single and multi-core scores for a CPU.

    Makes 4 search queries:
    1. sort=score, dir=desc → highest single-core (first result)
    2. sort=score, dir=asc  → lowest single-core (first result)
    3. sort=multicore_score, dir=desc → highest multi-core (first result)
    4. sort=multicore_score, dir=asc  → lowest multi-core (first result)
    """
    query = sanitize_query(cpu_name)
    result = {
        'cpu_id': cpu_id,
        'cpu_name': cpu_name,
        'query': query,
        'single_high': None,
        'single_low': None,
        'multi_high': None,
        'multi_low': None,
        'results_count': 0,
        'error': None,
    }

    queries = [
        ('score', 'desc', 'single_high'),
        ('score', 'asc', 'single_low'),
        ('multicore_score', 'desc', 'multi_high'),
        ('multicore_score', 'asc', 'multi_low'),
    ]

    for sort_field, direction, field in queries:
        try:
            url = SEARCH_URL.format(
                query=query,
                sort=sort_field,
                direction=direction,
            )
            page = await browser.get(url)
            await asyncio.sleep(5)  # Wait for page + Cloudflare

            # Verify page loaded
            title = await page.evaluate('document.title')
            if 'Just a moment' in title or 'Checking your browser' in title:
                result['error'] = f'Cloudflare blocked: {field}'
                return result

            html = await page.get_content()
            parsed = parse_search_results(html)

            if parsed:
                result[field] = parsed['single'] if 'single' in field else parsed['multi']
                if field == 'single_high':
                    result['results_count'] = parsed['total_count']
            else:
                result['error'] = f'No results for {field} ({query})'
                return result

            await asyncio.sleep(delay)

        except Exception as e:
            result['error'] = f'{field}: {str(e)}'
            return result

    return result


async def run_scraper(args):
    """Main scraper loop."""
    conn = sqlite3.connect(DB_PATH)
    init_schema(conn)

    cpus = get_cpu_names(conn, source=args.source, limit=args.limit)
    total = len(cpus)
    print(f"Scraping {total} CPUs (source={args.source or 'all'}, limit={args.limit})")
    print(f"Delay: {DELAY_BETWEEN_QUERIES}s between queries, {DELAY_BETWEEN_CPUS}s between CPUs")
    print()

    # Launch browser
    print("Launching Chrome...")
    browser = await uc.start(
        headless=False,
        browser_executable_path='/usr/bin/vivaldi',
        browser_args=['--no-first-run', '--no-default-browser-check'],
    )

    success = 0
    failures = 0
    errors = []

    for i, (cpu_id, cpu_name) in enumerate(cpus, 1):
        query = sanitize_query(cpu_name)
        print(f"[{i}/{total}] {cpu_name} → query: '{query}'")

        t0 = time.time()
        result = await scrape_cpu(browser, cpu_id, cpu_name)
        elapsed = time.time() - t0

        if result['error']:
            print(f"  ✗ {result['error']} ({elapsed:.1f}s)")
            failures += 1
            errors.append((cpu_name, result['error']))
        else:
            print(f"  ✓ single: {result['single_high']}/{result['single_low']}, "
                  f"multi: {result['multi_high']}/{result['multi_low']} "
                  f"({result['results_count']} results, {elapsed:.1f}s)")
            success += 1

            # Save to DB
            conn.execute("""
                INSERT INTO geekbench_search (cpu_id, cpu_name, single_high, single_low,
                                              multi_high, multi_low, results_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (cpu_id, cpu_name, result['single_high'], result['single_low'],
                  result['multi_high'], result['multi_low'], result['results_count']))
            conn.commit()

        # Delay between CPUs
        if i < total:
            await asyncio.sleep(DELAY_BETWEEN_CPUS)

    # Summary
    print(f"\n{'='*60}")
    print(f"Scrape complete:")
    print(f"  Success: {success}/{total}")
    print(f"  Failed:  {failures}/{total}")
    if errors:
        print(f"\nFailed CPUs:")
        for name, err in errors[:10]:
            print(f"  - {name}: {err}")
        if len(errors) > 10:
            print(f"  ... and {len(errors) - 10} more")

    conn.close()
    browser.stop()


def main():
    parser = argparse.ArgumentParser(description="Scrape Geekbench 6 search results")
    parser.add_argument("--limit", type=int, help="Limit to first N CPUs")
    parser.add_argument("--source", choices=["notebookcheck", "vray", "geekbench"],
                        help="Only scrape CPUs from this source")
    parser.add_argument("--delay", type=float, default=DELAY_BETWEEN_QUERIES,
                        help=f"Delay between queries (default: {DELAY_BETWEEN_QUERIES}s)")
    args = parser.parse_args()

    asyncio.run(run_scraper(args))


if __name__ == "__main__":
    main()
