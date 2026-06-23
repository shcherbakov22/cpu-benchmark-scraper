#!/usr/bin/env python3
"""Fast Geekbench 6 search scraper.

Uses nodriver once to bypass Cloudflare and get cookies,
then uses curl_cffi for all subsequent requests (no browser overhead).

~2-3s per CPU vs ~30s with full browser navigation.

Usage:
    python3 scrape_geekbench_fast.py                # scrape all CPUs
    python3 scrape_geekbench_fast.py --limit 100    # first 100 only
    python3 scrape_geekbench_fast.py --source notebookcheck  # only NB CPUs
"""
import argparse
import asyncio
import os
import re
import sqlite3
import time
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "benchmarks.sqlite")

SEARCH_URL = "https://browser.geekbench.com/v6/cpu/search?q={query}&sort={sort}&dir={direction}"

DELAY_BETWEEN_QUERIES = 1.5
DELAY_BETWEEN_CPUS = 2.0


def init_schema(conn):
    """Create geekbench_search table if not exists."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS geekbench_search (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            cpu_id          INTEGER NOT NULL REFERENCES cpus(id) ON DELETE CASCADE,
            cpu_name        TEXT NOT NULL,

            single_high     INTEGER,
            single_low      INTEGER,
            multi_high      INTEGER,
            multi_low       INTEGER,

            results_count   INTEGER,
            scraped_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_gb_search_cpu_id ON geekbench_search (cpu_id);
        CREATE INDEX IF NOT EXISTS idx_gb_search_cpu_name ON geekbench_search (cpu_name);
    """)
    conn.commit()


def get_cpu_names(conn, source=None, limit=None, skip=None):
    """Get CPU names to scrape."""
    query = "SELECT c.id, c.name FROM cpus c WHERE 1=1"
    params = []

    # Skip already scraped CPUs
    query += " AND NOT EXISTS (SELECT 1 FROM geekbench_search gs WHERE gs.cpu_id = c.id)"

    if source == "notebookcheck":
        query += " AND c.nb_id IS NOT NULL"
    elif source == "vray":
        query += " AND c.vray_id IS NOT NULL"
    elif source == "geekbench":
        query += " AND c.geekbench_id IS NOT NULL"

    query += " ORDER BY c.id"

    if skip:
        query += " LIMIT ? OFFSET ?"
        params.extend([skip, skip * (skip or 1)])
    elif limit:
        query += " LIMIT ?"
        params.append(limit)

    return conn.execute(query, params).fetchall()


def sanitize_query(name):
    """Convert CPU name to a Geekbench search query."""
    name = name.strip()

    # Apple Silicon: "Apple M4 Max 16-Core" → "M4 Max"
    m = re.search(r'(M\d+\s+(Max|Pro|Ultra))', name)
    if m:
        return m.group(1).strip()

    # Intel: i3-12100, i5-13600K, i7-13700K, i9-13900K
    m = re.search(r'(i[3-9]-\d+[A-Z]?)', name)
    if m:
        return m.group(1)

    # Intel Core Ultra
    m = re.search(r'(Core\s+Ultra\s+\d+\s+\d+[A-Z]?)', name)
    if m:
        return m.group(1)

    # AMD Ryzen: "Ryzen 5 5600X", "Ryzen 9 9955HX3D"
    m = re.search(r'(Ryzen\s+AI\s+\d+\s+[A-Z]+\s+\d+[A-Z]*|Ryzen\s+\d+\s+\d+[A-Z0-9]+)', name)
    if m:
        return m.group(1)

    # AMD Threadripper
    m = re.search(r'(Threadripper\s+(?:PRO\s+)?\d+[A-Z]+)', name)
    if m:
        return m.group(1)

    # AMD EPYC
    m = re.search(r'(EPYC\s+\d+[A-Z]+)', name)
    if m:
        return m.group(1)

    # Qualcomm
    m = re.search(r'(Snapdragon\s+[A-Z0-9]+(?:\s+[A-Za-z]+)?)', name)
    if m:
        return m.group(1)

    # Fallback: strip core counts and clock speeds
    clean = re.sub(r'\d+-Core.*', '', name).strip()
    clean = re.sub(r'\d+\s*MHz.*', '', clean).strip()
    clean = re.sub(r'\(\d+\s*cores?.*', '', clean).strip()
    parts = clean.split()
    if len(parts) >= 3:
        return ' '.join(parts[-3:])
    return ' '.join(parts[-2:]) if len(parts) >= 2 else parts[0]


def parse_search_results(html):
    """Parse Geekbench search results HTML. Returns first valid result."""
    soup = BeautifulSoup(html, 'html.parser')

    total_count = 0
    small = soup.select_one('h2 small')
    if small:
        m = re.search(r'(\d[\d,]*)', small.get_text())
        if m:
            total_count = int(m.group(1).replace(',', ''))

    rows = soup.select('.list-col-inner')
    for row in rows:
        scores = row.select('.list-col-text-score')
        if len(scores) >= 2:
            single = int(scores[0].get_text(strip=True))
            multi = int(scores[1].get_text(strip=True))
            if single > 0 and multi > 0:
                return {'single': single, 'multi': multi, 'total_count': total_count}

    return None


def get_cookies_with_browser():
    """Use nodriver once to bypass Cloudflare and get cookies."""
    print("Launching browser to get Cloudflare cookies...")

    async def _get():
        import nodriver as uc
        browser = await uc.start(
            headless=False,
            browser_executable_path='/usr/bin/vivaldi',
            browser_args=['--no-first-run', '--no-default-browser-check'],
        )
        page = await browser.get('https://browser.geekbench.com/')
        await asyncio.sleep(8)

        # Navigate to a search page to ensure cookies are set
        page2 = await browser.get('https://browser.geekbench.com/v6/cpu/search?q=i7-13700K&sort=score&dir=desc')
        await asyncio.sleep(6)

        # Extract ALL cookies (including HttpOnly) via CDP
        import nodriver.cdp as cdp
        # Use the browser's active tab to send CDP commands
        # First enable network domain
        await page.send(cdp.network.enable())
        # Get cookies for the current URL
        cookies_result = await page.send(cdp.network.get_cookies(urls=['https://browser.geekbench.com']))
        cookies = {}
        for c in cookies_result:
            key = c.domain + '/' + (c.path or '/')
            cookies[key] = {
                'name': c.name,
                'value': c.value,
                'domain': c.domain,
                'path': c.path or '/',
            }
        browser.stop()
        return cookies

    return asyncio.run(_get())


def make_session(cookies_data):
    """Create curl_cffi session with Cloudflare cookies."""
    session = curl_requests.Session(
        impersonate='chrome',
        timeout=30,
    )

    # Set cookies for geekbench domain
    cookie_str = '; '.join(
        f"{c['name']}={c['value']}"
        for c in cookies_data.values()
        if 'geekbench' in c.get('domain', '')
    )
    if cookie_str:
        session.headers['Cookie'] = cookie_str

    return session


def scrape_cpu(session, cpu_id, cpu_name, delay=DELAY_BETWEEN_QUERIES):
    """Scrape high/low scores for a CPU using HTTP requests."""
    query = sanitize_query(cpu_name)
    result = {
        'cpu_id': cpu_id,
        'cpu_name': cpu_name,
        'query': query,
        'single_high': None, 'single_low': None,
        'multi_high': None, 'multi_low': None,
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
            url = SEARCH_URL.format(query=query, sort=sort_field, direction=direction)
            resp = session.get(url)

            if resp.status_code != 200:
                result['error'] = f'{field}: HTTP {resp.status_code}'
                return result

            # Check for Cloudflare block
            if 'Just a moment' in resp.text or 'Checking your browser' in resp.text:
                result['error'] = f'Cloudflare blocked: {field}'
                return result

            parsed = parse_search_results(resp.text)
            if parsed:
                result[field] = parsed['single'] if 'single' in field else parsed['multi']
                if field == 'single_high':
                    result['results_count'] = parsed['total_count']
            else:
                result['error'] = f'No results for {field} ({query})'
                return result

            time.sleep(delay)

        except Exception as e:
            result['error'] = f'{field}: {str(e)}'
            return result

    return result


def main():
    parser = argparse.ArgumentParser(description="Fast Geekbench search scraper")
    parser.add_argument("--limit", type=int, help="Limit to first N CPUs")
    parser.add_argument("--source", choices=["notebookcheck", "vray", "geekbench"])
    parser.add_argument("--skip", type=int, help="Skip first N CPUs (for resuming)")
    parser.add_argument("--cookies-only", action="store_true", help="Just get cookies and exit")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    init_schema(conn)

    # Get cookies
    cookies_data = get_cookies_with_browser()
    print(f"Got {len(cookies_data)} cookies")

    if args.cookies_only:
        import json
        with open(os.path.join(BASE_DIR, 'gb_cookies.json'), 'w') as f:
            json.dump(cookies_data, f)
        print("Saved cookies to gb_cookies.json")
        conn.close()
        return

    # Create HTTP session
    session = make_session(cookies_data)

    cpus = get_cpu_names(conn, source=args.source, limit=args.limit, skip=args.skip)
    total = len(cpus)
    print(f"\nScraping {total} CPUs (source={args.source or 'all'}, limit={args.limit})")
    print()

    success = 0
    failures = 0
    errors = []
    requests_since_refresh = 0
    REFRESH_INTERVAL = 12  # Refresh cookies every N requests

    for i, (cpu_id, cpu_name) in enumerate(cpus, 1):
        # Refresh cookies periodically
        if requests_since_refresh >= REFRESH_INTERVAL:
            print("\n... Refreshing Cloudflare cookies ...")
            session.close()
            cookies_data = get_cookies_with_browser()
            session = make_session(cookies_data)
            requests_since_refresh = 0
            print(f"... Got {len(cookies_data)} new cookies, continuing ...\n")

        query = sanitize_query(cpu_name)
        print(f"[{i}/{total}] {cpu_name} → '{query}'", end=' ', flush=True)

        t0 = time.time()
        result = scrape_cpu(session, cpu_id, cpu_name)
        elapsed = time.time() - t0
        requests_since_refresh += 1

        if result['error']:
            print(f"✗ {result['error']} ({elapsed:.1f}s)")
            failures += 1
            errors.append((cpu_name, result['error']))
        else:
            print(f"✓ s:{result['single_high']}/{result['single_low']} "
                  f"m:{result['multi_high']}/{result['multi_low']} "
                  f"({result['results_count']} results, {elapsed:.1f}s)")
            success += 1

            conn.execute("""
                INSERT INTO geekbench_search (cpu_id, cpu_name, single_high, single_low,
                                              multi_high, multi_low, results_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (cpu_id, cpu_name, result['single_high'], result['single_low'],
                  result['multi_high'], result['multi_low'], result['results_count']))
            conn.commit()

        time.sleep(DELAY_BETWEEN_CPUS)

    print(f"\n{'='*60}")
    print(f"Scrape complete: {success}/{total} success, {failures}/{total} failed")
    if errors:
        print(f"\nFailed CPUs:")
        for name, err in errors[:10]:
            print(f"  - {name}: {err}")
        if len(errors) > 10:
            print(f"  ... and {len(errors) - 10} more")

    conn.close()
    session.close()


if __name__ == "__main__":
    main()
