#!/usr/bin/env python3
"""
Check domain availability using RDAP (Registration Data Access Protocol).

Usage:
    python check_domains.py domain1.tld domain2.tld ...
    python check_domains.py < domains.txt
    python check_domains.py --file domains.txt
    python check_domains.py --clear-cache  # Clear cached results

Results are cached in ~/.cache/domain-checker/cache.json
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# Cache settings
CACHE_DIR = Path.home() / ".cache" / "domain-checker"
CACHE_FILE = CACHE_DIR / "cache.json"
CACHE_TTL_SECONDS = 86400 * 7  # 7 days

Status = Literal["available", "taken", "unknown"]


@dataclass
class CacheEntry:
    status: Status
    timestamp: float


# RDAP bootstrap - maps TLDs to their RDAP servers
RDAP_SERVERS = {
    # Google TLDs
    "dev": "https://rdap.nic.google/rdap",
    "app": "https://rdap.nic.google/rdap",
    "page": "https://rdap.nic.google/rdap",
    "cloud": "https://rdap.nic.google/rdap",
    # Verisign
    "com": "https://rdap.verisign.com/com/v1",
    "net": "https://rdap.verisign.com/net/v1",
    # Others
    "org": "https://rdap.publicinterestregistry.org/rdap",
    "info": "https://rdap.afilias.net/rdap/info",
    "io": "https://rdap.nic.io",
    "ai": "https://rdap.nic.ai",
    "sh": "https://rdap.nic.sh",
    "me": "https://rdap.nic.me",
    "fun": "https://rdap.nic.fun",
    "club": "https://rdap.nic.club",
    "xyz": "https://rdap.nic.xyz",
    # Donuts TLDs (large registry for new gTLDs)
    **dict.fromkeys(
        [
            "systems",
            "services",
            "network",
            "technology",
            "agency",
            "solutions",
            "zone",
            "host",
            "space",
            "world",
            "life",
            "live",
            "rocks",
            "wtf",
            "lol",
            "fail",
            "computer",
            "partners",
            "capital",
            "land",
            "house",
            "energy",
            "run",
            "digital",
            "company",
            "group",
            "team",
            "works",
            "plus",
            "express",
            "media",
            "studio",
            "software",
            "engineering",
            "center",
            "directory",
            "support",
            "tools",
            "domains",
            "email",
            "academy",
            "training",
            "institute",
            "education",
            "school",
            "management",
            "marketing",
            "consulting",
            "finance",
            "ventures",
            "holdings",
            "investments",
            "enterprises",
            "industries",
            "international",
            "limited",
            "foundation",
        ],
        "https://rdap.donuts.co/rdap",
    ),
}


class DomainCache:
    def __init__(self):
        self.cache: dict[str, CacheEntry] = {}
        self._load()

    def _load(self):
        if CACHE_FILE.exists():
            try:
                data = json.loads(CACHE_FILE.read_text())
                now = time.time()
                for domain, entry in data.items():
                    if now - entry["timestamp"] < CACHE_TTL_SECONDS:
                        self.cache[domain] = CacheEntry(status=entry["status"], timestamp=entry["timestamp"])
            except (json.JSONDecodeError, KeyError):
                self.cache = {}

    def save(self):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        data = {domain: {"status": entry.status, "timestamp": entry.timestamp} for domain, entry in self.cache.items()}
        CACHE_FILE.write_text(json.dumps(data, indent=2))

    def get(self, domain: str) -> Status | None:
        entry = self.cache.get(domain)
        if entry and time.time() - entry.timestamp < CACHE_TTL_SECONDS:
            return entry.status
        return None

    def set(self, domain: str, status: Status):
        self.cache[domain] = CacheEntry(status=status, timestamp=time.time())

    def clear(self):
        self.cache = {}
        if CACHE_FILE.exists():
            CACHE_FILE.unlink()


def check_domain_rdap(domain: str) -> Status:
    """Check domain availability via RDAP."""
    tld = domain.rsplit(".", maxsplit=1)[-1]

    # Build list of RDAP URLs to try
    urls = []
    if tld in RDAP_SERVERS:
        urls.append(f"{RDAP_SERVERS[tld]}/domain/{domain}")
    urls.append(f"https://rdap.org/domain/{domain}")

    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/rdap+json, application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                if data.get("ldhName") or data.get("handle") or data.get("objectClassName"):
                    return "taken"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "available"
            continue
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
            continue

    return "unknown"


def check_domain(domain: str, cache: DomainCache) -> tuple[str, Status, bool]:
    """Check domain, using cache if available. Returns (domain, status, from_cache)."""
    domain = domain.strip().lower()
    if not domain or domain.startswith("#"):
        return domain, "unknown", False

    # Check cache first
    cached = cache.get(domain)
    if cached:
        return domain, cached, True

    # Query RDAP
    status = check_domain_rdap(domain)
    cache.set(domain, status)
    return domain, status, False


def parse_domains_file(filepath: Path) -> list[str]:
    """Parse domain file, skipping comments and empty lines."""
    domains = []
    for raw_line in filepath.read_text().splitlines():
        stripped = raw_line.strip()
        # Skip comments and empty lines
        if not stripped or stripped.startswith("#"):
            continue
        # Handle inline comments and extra info
        domain = stripped.split()[0].split("#")[0].strip()
        if domain and "." in domain:
            domains.append(domain)
    return domains


def main():
    parser = argparse.ArgumentParser(description="Check domain availability via RDAP")
    parser.add_argument("domains", nargs="*", help="Domains to check")
    parser.add_argument("--file", "-f", type=Path, help="File containing domains")
    parser.add_argument("--clear-cache", action="store_true", help="Clear cache and exit")
    parser.add_argument("--no-cache", action="store_true", help="Don't use cache")
    parser.add_argument("--workers", "-w", type=int, default=10, help="Parallel workers")
    args = parser.parse_args()

    cache = DomainCache()

    if args.clear_cache:
        cache.clear()
        print("Cache cleared.")
        return

    # Gather domains from all sources
    domains = list(args.domains)
    if args.file:
        domains.extend(parse_domains_file(args.file))
    if not sys.stdin.isatty():
        for raw_line in sys.stdin:
            stripped = raw_line.strip()
            if stripped and not stripped.startswith("#"):
                domain = stripped.split()[0].split("#")[0].strip()
                if domain and "." in domain:
                    domains.append(domain)

    # Deduplicate while preserving order
    seen = set()
    unique_domains = []
    for domain in domains:
        normalized = domain.lower().strip()
        if normalized not in seen and normalized:
            seen.add(normalized)
            unique_domains.append(normalized)
    domains = unique_domains

    if not domains:
        parser.print_help()
        return

    print(f"Checking {len(domains)} domains...\n")

    available = []
    taken = []
    unknown = []

    def check_with_cache(domain):
        if args.no_cache:
            status = check_domain_rdap(domain)
            return domain, status, False
        return check_domain(domain, cache)

    # Check domains in parallel
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(check_with_cache, d): d for d in domains}
        for future in as_completed(futures):
            domain, status, from_cache = future.result()
            cache_indicator = " (cached)" if from_cache else ""

            if status == "available":
                available.append(domain)
                print(f"✓ {domain}: AVAILABLE{cache_indicator}")
            elif status == "taken":
                taken.append(domain)
                print(f"✗ {domain}: taken{cache_indicator}")
            else:
                unknown.append(domain)
                print(f"? {domain}: unknown{cache_indicator}")

    # Save cache
    if not args.no_cache:
        cache.save()

    # Summary
    print(f"\n{'=' * 50}")
    print(f"SUMMARY: {len(available)} available, {len(taken)} taken, {len(unknown)} unknown")
    print(f"{'=' * 50}")

    if available:
        print(f"\n✓ AVAILABLE ({len(available)}):")
        for d in sorted(available):
            print(f"  {d}")

    if unknown:
        print(f"\n? UNKNOWN ({len(unknown)}):")
        for d in sorted(unknown):
            print(f"  {d}")


if __name__ == "__main__":
    main()
