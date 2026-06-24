#!/usr/bin/env python3
"""
Geekbench search scraper — Safari impersonation, no cookies needed.
Outputs to JSON for processing before DB import.

For each CPU: 2 searches (single desc, multi desc)
Gets page 1 (highest) and last page (lowest) from each.

Validation on search page:
- Score > 0
- CPU name matches query
- Core count reasonable (not VM with fewer cores)
- Clock > 1000 MHz (filters VMs)
- No QEMU/KVM/VirtualBox in visible text

Usage: python3 scrape_geekbench_safari.py [--limit N] [--resume]
"""
import sqlite3
import json
import re
import time
import sys
import urllib.parse
from curl_cffi import requests
from bs4 import BeautifulSoup

DB_PATH = "benchmarks.sqlite"
OUTPUT = "geekbench_search_results.json"
DELAY = 0.3

# Virtualization keywords to filter
VM_KEYWORDS = ['qemu', 'kvm', 'virtual', 'vmware', 'hyper-v', 'virtualbox', 'xen',
               'proxmox', 'bhyve', 'parallels']

def sanitize_name(name: str) -> str:
    """Convert DB CPU name to Geekbench search query."""
    q = name
    q = re.sub(r'^Intel\s+Core\s+', '', q, flags=re.I)
    q = re.sub(r'^Apple\s+', '', q, flags=re.I)
    q = re.sub(r'^AMD\s+(Ryzen|EPYC|Threadripper)\s+', r'\1 ', q, flags=re.I)
    q = re.sub(r'^Qualcomm\s+', '', q, flags=re.I)
    q = re.sub(r'Core\s+Ultra\s+(\d+)\s+(\d+[A-Z]*)\s*Plus\s*$', r'Core Ultra \1 \2', q, flags=re.I)
    q = re.sub(r'\s+(\d+)\s*(Core|Cores?)\s*$', '', q, flags=re.I)
    q = re.sub(r'[^\w\s\-]', ' ', q)
    q = re.sub(r'\s+', ' ', q).strip()
    return q

def parse_result_item(item) -> dict:
    """Parse a single search result item."""
    result = {}

    # CPU model info
    model = item.select_one(".list-col-model")
    if model:
        text = model.get_text('\n')
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        result['cpu_name'] = lines[0] if len(lines) > 0 else ""
        clock_match = re.search(r'(\d+)\s*MHz', lines[1] if len(lines) > 1 else "")
        result['clock_mhz'] = int(clock_match.group(1)) if clock_match else 0
        cores_match = re.search(r'(\d+)\s*cores?', lines[2] if len(lines) > 2 else "", re.I)
        result['cores'] = int(cores_match.group(1)) if cores_match else 0

    # System name
    sys_link = item.select_one("a[href*='/v6/cpu/']")
    if sys_link:
        result['system'] = sys_link.get_text(strip=True)
        result['result_id'] = int(sys_link.get('href').split('/')[-1])

    # Date & user
    # Format: "May 21, 2024oc_donz1ral" (date + username concatenated)
    date_col = item.select_one(".col-6 .list-col-text")
    if date_col:
        text = date_col.get_text(strip=True)
        date_match = re.match(r'([A-Za-z]+\s+\d{1,2},?\s+\d{4})', text)
        result['date'] = date_match.group(1) if date_match else text
        if date_match:
            result['user'] = text[date_match.end():]
        else:
            result['user'] = ""

    # Platform
    platform_cols = item.select(".col-6")
    for col in platform_cols:
        subtitle = col.select_one(".list-col-subtitle")
        if subtitle and subtitle.get_text(strip=True) == "Platform":
            text_el = col.select_one(".list-col-text")
            result['platform'] = text_el.get_text(strip=True) if text_el else ""
            break

    # Scores
    scores = item.select(".list-col-text-score")
    result['single'] = int(scores[0].get_text(strip=True)) if len(scores) > 0 else 0
    result['multi'] = int(scores[1].get_text(strip=True)) if len(scores) > 1 else 0

    return result

def validate_result(result: dict, db_cores: int = None) -> tuple:
    """Validate a parsed result. Returns (valid, reason)."""
    # Score > 0
    if result.get('single', 0) <= 0 or result.get('multi', 0) <= 0:
        return False, "zero_score"

    # Clock sanity (> 1000 MHz filters VMs)
    if result.get('clock_mhz', 0) < 1000:
        return False, "low_clock"

    # Core count sanity
    cores = result.get('cores', 0)
    if cores < 1:
        return False, "no_cores"

    # If we know the expected core count, check it's not a VM with fewer cores
    if db_cores and cores < db_cores * 0.5:
        return False, f"vm_cores({cores}<{db_cores})"

    # Check for VM keywords in visible text
    text = ' '.join(str(v) for v in result.values()).lower()
    for kw in VM_KEYWORDS:
        if kw in text:
            return False, f"vm_keyword({kw})"

    return True, "ok"

def scrape_cpu(cpu_id: int, cpu_name: str, db_cores: int) -> dict:
    """Scrape high/low scores for a CPU. Returns dict with all data."""
    q = sanitize_name(cpu_name)

    result = {
        "cpu_id": cpu_id,
        "cpu_name": cpu_name,
        "query": q,
        "db_cores": db_cores,
    }

    for score_type in ["single", "multi"]:
        # Page 1 (highest scores)
        url = (
            f"https://browser.geekbench.com/v6/cpu/search"
            f"?q={urllib.parse.quote(q)}&sort=score&score_type={score_type}&order=desc"
        )
        r = requests.get(url, impersonate="safari", timeout=20)
        if r.status_code != 200:
            result[f"{score_type}_error"] = f"http_{r.status_code}"
            continue

        soup = BeautifulSoup(r.text, "html.parser")

        # Check for no results
        if "no results" in r.text.lower() or "not found" in r.text.lower():
            result[f"{score_type}_error"] = "no_results"
            continue

        items = soup.select(".list-col-inner")
        if not items:
            result[f"{score_type}_error"] = "no_items"
            continue

        # Parse first item (highest score)
        high = parse_result_item(items[0])
        valid, reason = validate_result(high, db_cores)
        high['valid'] = valid
        high['validation'] = reason

        # Get total pages
        match = re.search(r'(\d+)\s+results?', r.text, re.I)
        total = int(match.group(1)) if match else 0
        last_page = max(1, (total + 29) // 30)

        if last_page > 1:
            time.sleep(DELAY)
            url_last = url + f"&page={last_page}"
            r2 = requests.get(url_last, impersonate="safari", timeout=20)
            if r2.status_code == 200:
                soup2 = BeautifulSoup(r2.text, "html.parser")
                items2 = soup2.select(".list-col-inner")
                if items2:
                    low = parse_result_item(items2[0])
                    valid, reason = validate_result(low, db_cores)
                    low['valid'] = valid
                    low['validation'] = reason
                else:
                    low = {"error": "no_items_on_last_page"}
            else:
                low = {"error": f"http_{r2.status_code}"}
        else:
            low = high.copy()
            low['valid'] = high['valid']
            low['validation'] = high['validation']

        result[f"{score_type}_high"] = high
        result[f"{score_type}_low"] = low
        result[f"{score_type}_total_results"] = total

        time.sleep(DELAY)

    return result

def main():
    args = sys.argv[1:]
    limit = None
    resume = "--resume" in args

    for i, arg in enumerate(args):
        if arg == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])

    conn = sqlite3.connect(DB_PATH)

    query = """
        SELECT c.id, c.name, c.cores
        FROM cpus c
        WHERE c.id NOT IN (SELECT cpu_id FROM geekbench_search)
        ORDER BY c.id
    """
    rows = conn.execute(query).fetchall()
    conn.close()

    if limit:
        rows = rows[:limit]
    total = len(rows)
    print(f"CPUs to scrape: {total}")

    # Load existing results if resuming
    existing = {}
    if resume and not limit:
        try:
            with open(OUTPUT) as f:
                data = json.load(f)
                existing = {r['cpu_id']: r for r in data}
                rows = [r for r in rows if r[0] not in existing]
                total = len(rows)
                print(f"Resuming: {len(existing)} already done, {total} remaining")
        except FileNotFoundError:
            pass

    results = list(existing.values())
    success = 0
    failed = 0
    no_results = 0

    for i, (cpu_id, cpu_name, db_cores) in enumerate(rows, 1):
        t0 = time.time()
        print(f"\n[{i}/{total}] {cpu_name} ({db_cores}C) → '{sanitize_name(cpu_name)}'",
              end="", flush=True)

        result = scrape_cpu(cpu_id, cpu_name, db_cores or 0)
        elapsed = time.time() - t0

        # Check outcomes
        sh = result.get("single_high", {})
        sl = result.get("single_low", {})
        mh = result.get("multi_high", {})
        ml = result.get("multi_low", {})

        if result.get("single_error") == "no_results" or result.get("single_error"):
            print(f" ✗ {result.get('single_error', 'error')} ({elapsed:.1f}s)", flush=True)
            no_results += 1
            results.append(result)
            continue

        # Check if valid scores found
        if sh.get('valid') and mh.get('valid'):
            print(f" ✓ s:{sh.get('single')}/{sl.get('single','?')} "
                  f"m:{mh.get('multi')}/{ml.get('multi','?')} ({elapsed:.1f}s)", flush=True)
            success += 1
        else:
            reasons = []
            if not sh.get('valid', True):
                reasons.append(f"sh:{sh.get('validation','?')}")
            if not mh.get('valid', True):
                reasons.append(f"mh:{mh.get('validation','?')}")
            print(f" ⚠ invalid: {', '.join(reasons)} ({elapsed:.1f}s)", flush=True)
            failed += 1

        results.append(result)

        # Save periodically
        if i % 50 == 0:
            with open(OUTPUT, 'w') as f:
                json.dump(results, f, indent=2)
            print(f"  → Saved {len(results)} results to {OUTPUT}")

    # Final save
    with open(OUTPUT, 'w') as f:
        json.dump(results, f, indent=2)

    print(f"\n{'='*60}")
    print(f"Done! Valid: {success}, Invalid: {failed}, Not found: {no_results}")
    print(f"Saved {len(results)} results to {OUTPUT}")

if __name__ == "__main__":
    main()
