#!/usr/bin/env python3
"""Fast Geekbench 6 search scraper using curl_cffi + browser cookies.

Uses the logged-in browser session to extract cookies, then uses curl_cffi
for fast HTTP requests. ~6s per CPU vs ~28s with full browser navigation.

Requires: browser on port 9222 with active Geekbench session.
Run: vivaldi --remote-debugging-port=9222

Usage:
    python3 scrape_geekbench_fast.py              # scrape all CPUs
    python3 scrape_geekbench_fast.py --limit 100  # first 100 only
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import websockets
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "benchmarks.sqlite")
SEARCH_URL = "https://browser.geekbench.com/v6/cpu/search?q={query}&sort={sort}&dir={direction}"
CDP_URL = "ws://127.0.0.1:9222/devtools/browser"

REFRESH_INTERVAL = 15  # Refresh cookies every N requests


def init_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS geekbench_search (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cpu_id INTEGER NOT NULL REFERENCES cpus(id) ON DELETE CASCADE,
            cpu_name TEXT NOT NULL,
            single_high INTEGER, single_low INTEGER,
            multi_high INTEGER, multi_low INTEGER,
            results_count INTEGER,
            scraped_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_gb_search_cpu_id ON geekbench_search (cpu_id);
        CREATE INDEX IF NOT EXISTS idx_gb_search_cpu_name ON geekbench_search (cpu_name);
    """)
    conn.commit()


def get_cpu_names(conn, source=None, limit=None):
    query = "SELECT c.id, c.name FROM cpus c WHERE NOT EXISTS (SELECT 1 FROM geekbench_search gs WHERE gs.cpu_id = c.id)"
    if source == "notebookcheck": query += " AND c.nb_id IS NOT NULL"
    elif source == "vray": query += " AND c.vray_id IS NOT NULL"
    elif source == "geekbench": query += " AND c.geekbench_id IS NOT NULL"
    query += " ORDER BY c.id"
    if limit: query += " LIMIT ?"
    return conn.execute(query, (limit,) if limit else ()).fetchall()


def sanitize_query(name):
    name = name.strip()
    m = re.search(r'(M\d+\s+(Max|Pro|Ultra))', name)
    if m: return m.group(1).strip()
    m = re.search(r'(i[3-9]-\d+[A-Z]?)', name)
    if m: return m.group(1)
    m = re.search(r'(Core\s+Ultra\s+\d+\s+\d+[A-Z]?)', name)
    if m: return m.group(1)
    m = re.search(r'(Ryzen\s+AI\s+\d+\s+[A-Z]+\s+\d+[A-Z]*|Ryzen\s+\d+\s+\d+[A-Z0-9]+)', name)
    if m: return m.group(1)
    m = re.search(r'(Threadripper\s+(?:PRO\s+)?\d+[A-Z]+)', name)
    if m: return m.group(1)
    m = re.search(r'(EPYC\s+\d+[A-Z]+)', name)
    if m: return m.group(1)
    m = re.search(r'(Snapdragon\s+[A-Z0-9]+(?:\s+[A-Za-z]+)?)', name)
    if m: return m.group(1)
    clean = re.sub(r'\d+-Core.*', '', name).strip()
    clean = re.sub(r'\d+\s*MHz.*', '', clean).strip()
    clean = re.sub(r'\(\d+\s*cores?.*', '', clean).strip()
    parts = clean.split()
    if len(parts) >= 3: return ' '.join(parts[-3:])
    return ' '.join(parts[-2:]) if len(parts) >= 2 else parts[0]


def parse_result(html):
    soup = BeautifulSoup(html, 'html.parser')
    total = 0
    small = soup.select_one('h2 small')
    if small:
        m = re.search(r'(\d[\d,]*)', small.get_text())
        if m: total = int(m.group(1).replace(',', ''))
    for row in soup.select('.list-col-inner'):
        scores = row.select('.list-col-text-score')
        if len(scores) >= 2:
            single = int(scores[0].get_text(strip=True))
            multi = int(scores[1].get_text(strip=True))
            if single > 100 and multi > 200:
                return single, multi, total
    return None, None, 0


def extract_cookies_from_browser():
    """Extract cookies from browser via CDP."""
    import asyncio

    async def _extract():
        # Find browser WebSocket URL
        import urllib.request
        with urllib.request.urlopen('http://127.0.0.1:9222/json/version') as r:
            version = json.loads(r.read())
        ws_url = version['webSocketDebuggerUrl']

        async with websockets.connect(ws_url) as ws:
            # Get targets
            await ws.send(json.dumps({'id': 1, 'method': 'Target.getTargets'}))
            targets = json.loads(await ws.recv())
            tabs = targets.get('result', {}).get('targetInfos', [])

            for t in tabs:
                if 'geekbench' in t.get('url', ''):
                    tid = t['targetId']
                    # Attach
                    await ws.send(json.dumps({
                        'id': 2, 'method': 'Target.attachToTarget',
                        'params': {'targetId': tid, 'flatten': True}
                    }))
                    while True:
                        msg = json.loads(await ws.recv())
                        if 'id' in msg and msg['id'] == 2:
                            session_id = msg['result']['sessionId']
                            break

                    # Enable network
                    await ws.send(json.dumps({
                        'id': 3, 'method': 'Network.enable',
                        'params': {}, 'sessionId': session_id
                    }))
                    await ws.recv()

                    # Get cookies
                    await ws.send(json.dumps({
                        'id': 4, 'method': 'Network.getCookies',
                        'params': {'urls': ['https://browser.geekbench.com']},
                        'sessionId': session_id
                    }))
                    resp = json.loads(await ws.recv())
                    cookies = resp.get('result', {}).get('cookies', [])

                    cookie_str = '; '.join(
                        f"{c['name']}={c['value']}"
                        for c in cookies if 'geekbench' in c.get('domain', '')
                    )
                    return cookie_str
            return None

    return asyncio.run(_extract())


def main():
    parser = argparse.ArgumentParser(description="Fast Geekbench search scraper")
    parser.add_argument("--limit", type=int, help="Limit to first N CPUs")
    parser.add_argument("--source", choices=["notebookcheck", "vray", "geekbench"])
    parser.add_argument("--cookie-file", default=os.path.join(BASE_DIR, "gb_cookie_string.txt"),
                        help="Path to cookie string file")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    init_schema(conn)

    # Load or extract cookies
    if os.path.exists(args.cookie_file):
        with open(args.cookie_file) as f:
            cookie_str = f.read()
        print(f"Loaded cookies from {args.cookie_file} ({len(cookie_str)} chars)")
    else:
        print("No cookie file found. Extracting from browser...")
        cookie_str = extract_cookies_from_browser()
        if not cookie_str:
            print("ERROR: Could not extract cookies. Make sure Vivaldi is running with --remote-debugging-port=9222")
            sys.exit(1)
        with open(args.cookie_file, 'w') as f:
            f.write(cookie_str)
        print(f"Extracted cookies ({len(cookie_str)} chars)")

    # Create session
    session = curl_requests.Session(impersonate='chrome131', timeout=30)
    session.headers['Cookie'] = cookie_str

    # Warm-up request
    session.get('https://browser.geekbench.com/')

    cpus = get_cpu_names(conn, source=args.source, limit=args.limit)
    total = len(cpus)
    print(f"\nScraping {total} CPUs (source={args.source or 'all'}, limit={args.limit})")
    print(f"Refreshing cookies every {REFRESH_INTERVAL} requests\n")

    success = 0
    failures = 0
    errors = []
    requests_since_refresh = 0
    t_start = time.time()

    for i, (cpu_id, cpu_name) in enumerate(cpus, 1):
        # Refresh cookies periodically
        if requests_since_refresh >= REFRESH_INTERVAL:
            print(f"\n... Refreshing cookies ({i}/{total}) ...")
            new_cookies = extract_cookies_from_browser()
            if new_cookies:
                session.headers['Cookie'] = new_cookies
                with open(args.cookie_file, 'w') as f:
                    f.write(new_cookies)
                session.get('https://browser.geekbench.com/')  # Warm-up
                print(f"... Refreshed ({len(new_cookies)} chars)\n")
            else:
                print("... Warning: Could not refresh cookies\n")
            requests_since_refresh = 0

        query = sanitize_query(cpu_name)
        print(f"[{i}/{total}] {cpu_name} → '{query}'", end=' ', flush=True)

        t0 = time.time()
        result = {'cpu_id': cpu_id, 'cpu_name': cpu_name, 'query': query,
                  'single_high': None, 'single_low': None,
                  'multi_high': None, 'multi_low': None,
                  'results_count': 0, 'error': None}

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
                    result['error'] = f'HTTP {resp.status_code}: {field}'
                    break

                single, multi, total_count = parse_result(resp.text)
                if single is None:
                    result['error'] = f'No valid results for {field} ({query})'
                    break

                result[field] = single if 'single' in field else multi
                if field == 'single_high':
                    result['results_count'] = total_count

            except Exception as e:
                result['error'] = f'{field}: {str(e)}'
                break

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

    elapsed_total = time.time() - t_start
    print(f"\n{'='*60}")
    print(f"Done: {success}/{total} success, {failures}/{total} failed")
    print(f"Time: {elapsed_total/3600:.1f}h ({elapsed_total/60:.0f}min), "
          f"Avg: {elapsed_total/max(total,1):.1f}s/CPU")
    if errors:
        print(f"\nFailed:")
        for name, err in errors[:10]:
            print(f"  - {name}: {err}")
        if len(errors) > 10:
            print(f"  ... and {len(errors) - 10} more")

    conn.close()


if __name__ == "__main__":
    main()
