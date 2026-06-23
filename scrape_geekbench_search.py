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
    - Intel: extract model (i7-13700K)
    - AMD: extract model (Ryzen 5 5600X, Threadripper 3960X)
    - Apple: extract chip name without core count (M4 Max, M3 Pro)
    - Qualcomm: extract chip name (Snapdragon X Elite)
    """
    name = name.strip()

    # Apple Silicon: "Apple M4 Max 16-Core" → "M4 Max"
    #                "Apple M3 Pro" → "M3 Pro"
    m = re.search(r'(M\d+\s+(Max|Pro|Ultra))', name)
    if m:
        return m.group(1).strip()

    # Intel: i3-12100, i5-13600K, i7-13700K, i9-13900K, Core Ultra 7 265K
    m = re.search(r'(i[3-9]-\d+[A-Z]?)', name)
    if m:
        return m.group(1)

    # Intel Core Ultra
    m = re.search(r'(Core\s+Ultra\s+\d+\s+\d+[A-Z]?)', name)
    if m:
        return m.group(1)

    # AMD Ryzen: "Ryzen 5 5600X", "Ryzen 7 5800X", "Ryzen AI 9 HX 370"
    m = re.search(r'(Ryzen\s+AI\s+\d+\s+[A-Z]?\d+[A-Z]?|Ryzen\s+\d+\s+\d+[A-Z]?)', name)
    if m:
        return m.group(1)

    # AMD Threadripper: "Threadripper 3960X", "Threadripper PRO 7995WX"
    m = re.search(r'(Threadripper\s+(?:PRO\s+)?\d+[A-Z]?)', name)
    if m:
        return m.group(1)

    # AMD EPYC: "EPYC 9654"
    m = re.search(r'(EPYC\s+\d+[A-Z]?)', name)
    if m:
        return m.group(1)

    # Qualcomm: "Snapdragon X Elite", "Snapdragon 8 Gen 3"
    m = re.search(r'(Snapdragon\s+[A-Z0-9]+(?:\s+[A-Za-z]+)?)', name)
    if m:
        return m.group(1)

    # Fallback: use last 2-3 words, strip core counts
    # "AMD Ryzen 5 9600X 4500 MHz (6 cores)" → "Ryzen 5 9600X"
    clean = re.sub(r'\d+-Core.*', '', name).strip()
    clean = re.sub(r'\d+\s*MHz.*', '', clean).strip()
    clean = re.sub(r'\(\d+\s*cores?.*', '', clean).strip()
    parts = clean.split()
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
        success = False
        for attempt in range(3):  # Retry up to 3 times on Cloudflare block
            try:
                url = SEARCH_URL.format(
                    query=query,
                    sort=sort_field,
                    direction=direction,
                )
                page = await browser.get(url)
                await asyncio.sleep(6)  # Wait for page + Cloudflare

                # Verify page loaded
                title = await page.evaluate('document.title')
                if 'Just a moment' in title or 'Checking your browser' in title:
                    if attempt < 2:
                        print(f"    (Cloudflare challenge on {field}, retry {attempt+1}...)")
                        await asyncio.sleep(10 + attempt * 5)  # Longer wait on retry
                        continue
                    else:
                        result['error'] = f'Cloudflare blocked after 3 retries: {field}'
                        return result

                html = await page.get_content()
                parsed = parse_search_results(html)

                if parsed:
                    result[field] = parsed['single'] if 'single' in field else parsed['multi']
                    if field == 'single_high':
                        result['results_count'] = parsed['total_count']
                    success = True
                else:
                    result['error'] = f'No results for {field} ({query})'
                    return result

                break  # Success, exit retry loop

            except Exception as e:
                if attempt < 2:
                    print(f"    (Error on {field}: {e}, retry {attempt+1}...)")
                    await asyncio.sleep(5)
                    continue
                result['error'] = f'{field}: {str(e)}'
                return result

        if not success:
            result['error'] = f'{field}: all retries exhausted'
            return result

        await asyncio.sleep(delay)

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
