#!/usr/bin/env python3
"""Download images from a PUBLIC Google Photos share link.

Works with both forms of share URL:
  - short:  https://photos.app.goo.gl/XXXXXXXX
  - full:   https://photos.google.com/share/<ALBUM_ID>?key=<KEY>
  - single: https://photos.google.com/share/<ALBUM_ID>/photo/<PHOTO_ID>?key=<KEY>

Strategy (no API, no auth, stdlib only):
  1. Follow the share link to the album page HTML.
  2. Scrape the per-photo page paths (/share/<ALBUM>/photo/<PHOTO_ID>?key=<KEY>).
  3. Open each photo page and scrape its lh3.googleusercontent.com base URL.
  4. Append a size suffix to that base URL and download the bytes.

Only works for albums shared by *link* (the URL carries a `key=` token). Albums
shared privately to specific Google accounts return a login wall and cannot be
read this way.

Usage:
  python3 fetch_google_photos.py "<share_url>" [--out DIR] [--width N | --original] [--list]

Examples:
  python3 fetch_google_photos.py "https://photos.app.goo.gl/abc123" --out ./photos --width 2048
  python3 fetch_google_photos.py "<url>" --original          # full-resolution originals
  python3 fetch_google_photos.py "<url>" --list              # print direct image URLs, download nothing
"""

import argparse
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

# An lh3 base URL: https://lh3.googleusercontent.com/pw/<token>   (token may contain - _ and /)
# The size suffix begins at "=" (e.g. =s250-k-no), which is NOT in this class, so the
# match naturally stops at the base.
IMG_BASE_RE = re.compile(r"https://lh3\.googleusercontent\.com/[\w/-]+")
PHOTO_ID_RE = re.compile(r"/photo/([\w-]+)")
SHARE_ID_RE = re.compile(r"/share/([\w-]+)")
KEY_RE = re.compile(r"[?&]key=([\w-]+)")


def fetch(url: str, timeout: int = 30) -> tuple[str, str]:
    """GET a URL (following redirects); return (decoded text, final resolved URL)."""
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace"), resp.geturl()


def fetch_bytes(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_album(share_url: str):
    """Return (final_url, album_id, key, [photo_ids]) from an album OR single-photo URL."""
    html, final_url = fetch(share_url)

    album_id = None
    m = SHARE_ID_RE.search(final_url) or SHARE_ID_RE.search(html)
    if m:
        album_id = m.group(1)

    key = None
    m = KEY_RE.search(final_url) or KEY_RE.search(html)
    if m:
        key = m.group(1)

    # If the resolved URL is itself a single photo, just use that one id.
    single = PHOTO_ID_RE.search(final_url)
    if single:
        return final_url, album_id, key, [single.group(1)]

    # Otherwise scrape all photo ids from the album page, de-duped, order preserved.
    ids, seen = [], set()
    for pid in PHOTO_ID_RE.findall(html):
        if pid not in seen:
            seen.add(pid)
            ids.append(pid)
    return final_url, album_id, key, ids


def photo_page_url(album_id: str, key: str | None, photo_id: str) -> str:
    base = f"https://photos.google.com/share/{album_id}/photo/{photo_id}"
    return f"{base}?key={key}" if key else base


def base_image_url(photo_url: str):
    """Open a photo page and return its bare lh3 base URL (no size suffix)."""
    html, _ = fetch(photo_url)
    m = IMG_BASE_RE.search(html)
    return m.group(0) if m else None


def sized(base: str, width: int | None, original: bool) -> str:
    """Append the right size suffix to an lh3 base URL.

    - original=True  -> '=d'      (downloads the original bytes, incl. video)
    - width=N        -> '=wN'     (longest-dimension N, preserves aspect ratio)
    The '-no' guard avoids server-side upscaling / extra processing for stills.
    """
    if original:
        return base + "=d"
    return base + f"=w{width}-no"


def main():
    ap = argparse.ArgumentParser(description="Download images from a public Google Photos share link.")
    ap.add_argument("url", help="Google Photos share link (short or full)")
    ap.add_argument("--out", type=Path, default=Path("photos"), help="output directory")
    ap.add_argument("--width", type=int, default=2048, help="longest-side pixels for viewable JPEGs")
    ap.add_argument(
        "--original", action="store_true", help="download full-resolution originals instead of resized JPEGs"
    )
    ap.add_argument("--list", action="store_true", help="print direct image URLs and exit without downloading")
    args = ap.parse_args()

    try:
        final_url, album_id, key, photo_ids = parse_album(args.url)
    except urllib.error.URLError as e:
        sys.exit(f"ERROR: could not open share link: {e}")

    if not photo_ids:
        sys.exit(
            "ERROR: no photos found. The link may be private, expired, or not a "
            "link-shared album. Confirm it opens in an incognito browser window."
        )

    print(f"Resolved: {final_url}")
    print(f"Album: {album_id or '(single photo)'}   Photos: {len(photo_ids)}")

    args.out.mkdir(parents=True, exist_ok=True)
    results = []
    for i, pid in enumerate(photo_ids, 1):
        purl = photo_page_url(album_id, key, pid) if album_id else final_url
        base = base_image_url(purl)
        if not base:
            print(f"  [{i}/{len(photo_ids)}] {pid}: could not extract image URL (skipped)")
            continue
        img_url = sized(base, args.width, args.original)
        results.append((i, pid, img_url))
        if args.list:
            print(img_url)
            continue
        ext = "jpg"  # =wN renders JPEG; =d originals keep their type but jpg is a safe default
        dest = args.out / f"photo_{i:02d}.{ext}"
        try:
            data = fetch_bytes(img_url)
            dest.write_bytes(data)
            print(f"  [{i}/{len(photo_ids)}] {pid}: {len(data) // 1024} KB -> {dest}")
        except urllib.error.URLError as e:
            print(f"  [{i}/{len(photo_ids)}] {pid}: download failed ({e})")

    if not args.list:
        print(f"\nDone. {len(results)} image URL(s) resolved into {args.out}/")
        print("View the files (e.g. with the `view` tool) to inspect them.")


if __name__ == "__main__":
    main()
