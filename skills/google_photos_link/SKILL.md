---
name: google-photos-link
description: >-
  Download and view photos from a public Google Photos share link
  (photos.app.goo.gl or photos.google.com/share). Use whenever a Google
  Photos link should be viewed, described, or analyzed — web_fetch alone
  returns album metadata, never the pixels.
---

# Google Photos Link Access

## Why this skill exists

A Google Photos share link does **not** point at an image file. It points at a
JavaScript web app. Calling `web_fetch` on the link returns only the album
_shell_: a title, an `og:image` thumbnail, and a list of links to per-photo
pages. It does **not** give you the photos themselves. To actually see the
images you have to walk one extra layer: open each photo's page, pull out its
direct `lh3.googleusercontent.com` image URL, and download the bytes. This skill
encodes that walk so you don't rediscover it each time.

## Quickest path: run the script

A tested helper does the whole walk. Prefer it over doing the steps by hand.

```bash
# Download viewable JPEGs (longest side 2048px) into ./photos/
python3 scripts/fetch_google_photos.py "<share_url>" --out ./photos --width 2048

# Then open them to actually look:
#   view ./photos/photo_01.jpg   (etc.)
```

Other modes:

```bash
python3 scripts/fetch_google_photos.py "<url>" --original   # full-resolution originals (=d)
python3 scripts/fetch_google_photos.py "<url>" --list       # print direct image URLs, download nothing
```

The script accepts the short link, the full album link, or a single-photo link.
After it finishes, **view the downloaded files** — downloading is not the goal;
seeing the images is. Report what you see.

## What the link looks like (so you can sanity-check it)

| Form         | Example                                                           |
| ------------ | ----------------------------------------------------------------- |
| Short        | `https://photos.app.goo.gl/C3aVzvb4GpZ9NA2e8`                     |
| Full album   | `https://photos.google.com/share/AF1Qip…?key=RDdF…`               |
| Single photo | `https://photos.google.com/share/AF1Qip…/photo/AF1Qip…?key=RDdF…` |

The `key=` token is the share credential. If a link has no `key`, it is almost
certainly a private album and this approach will hit a login wall.

## The manual method (for debugging, or if the script breaks)

Do this by hand only when the script fails and you need to see where.

1. **Resolve the link.** `web_fetch` (or `curl -sL`) the share URL. The short
   `goo.gl` link 302-redirects to `photos.google.com/share/<ALBUM_ID>?key=<KEY>`.
   The returned HTML's `og:title` tells you the album name and photo count.

2. **Collect photo IDs from the album page.** The album HTML contains one link
   per photo of the form `/share/<ALBUM_ID>/photo/<PHOTO_ID>?key=<KEY>`. Extract
   every `<PHOTO_ID>` and de-duplicate while preserving order:
   `grep -oE '/photo/[A-Za-z0-9_-]+'`. (The album page does **not** contain the
   full-size image URLs — only the cover, via `og:image`. That's why step 3 is
   needed.)

3. **Get each photo's direct image URL.** Fetch each photo page. Its HTML embeds
   the real image at a thumbnail size, e.g.
   `https://lh3.googleusercontent.com/pw/<TOKEN>=s250-k-no`. Grab the base
   (everything before `=`):
   `grep -oE 'https://lh3\.googleusercontent\.com/[A-Za-z0-9_/-]+' | head -1`.

4. **Resize via the URL suffix, then download.** The `lh3` base URL is a
   resizer. Append a suffix and `curl` the result:
   - `=w2048-no` → longest side 2048px, aspect preserved, **no upscaling**
     (`-no` is what prevents upscaling and extra server processing).
   - `=w1280-no`, `=s1024`, `=w1920-h1080` → other size options.
   - `=d` → the **original** bytes at full quality and native dimensions
     (also the only way to pull the original of a video).

5. **View the files.** Open each downloaded JPEG with the `view` tool and
   describe / analyze as asked.

## Picking a size

- **Just viewing / describing:** `--width 2048` is plenty and keeps files small.
- **Reading fine detail (text, engraving, small labels):** try `--width 4096`,
  or `--original` to avoid re-compression artifacts. Note `=w{N}-no` never
  upscales, so for an image whose original is smaller than N you'll get the
  original size regardless.
- **User wants the actual files to keep:** `--original`.

## Limitations — know these before promising results

- **Public link-shared albums only.** If the album was shared privately to
  specific Google accounts (no `key=` in the URL), the pages return a sign-in
  wall and there is nothing to scrape. Tell the user it needs to be shared "by
  link" / "anyone with the link."
- **Links can expire or be revoked.** The owner can turn off link sharing at any
  time, after which the same URL stops working.
- **Videos:** the per-photo page exposes a poster frame for stills easily; for
  video, `=d` on the base may fetch the original file, but playback/streaming
  variants are out of scope here.
- **Consent interstitials:** in some regions Google serves a cookie-consent page
  to non-browser clients. If the script returns 0 photos but the link works in a
  browser, fall back to `curl -sL` with a browser `User-Agent` and inspect the
  HTML, or have the user open it in an incognito window to confirm it's public.
- **Respect privacy.** Only fetch links the user has supplied or clearly wants
  opened. Don't enumerate or guess album/photo IDs.

## Troubleshooting

| Symptom                                        | Likely cause                                | Fix                                                                              |
| ---------------------------------------------- | ------------------------------------------- | -------------------------------------------------------------------------------- |
| "no photos found"                              | private album / missing `key`               | Confirm the link opens in an incognito window; ask owner to share by link        |
| 0 photos but link works in browser             | consent page or odd UA handling             | Re-run with a browser `User-Agent`; inspect raw HTML for a redirect/consent form |
| Downloaded file isn't a valid image            | grabbed an interstitial, not the image      | Check `file <name>`; re-extract the `lh3` base from the photo page               |
| Image looks low-res                            | size suffix too small, or original is small | Use `--width 4096` or `--original`; remember `-no` won't upscale                 |
| `Syntax error: "(" unexpected` in a shell loop | script ran under `/bin/sh`, not bash        | Use the Python helper, or write bash to a file and run `bash file.sh`            |
