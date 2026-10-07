# IKEA 3D model API reference

Reference for the IKEA internal/CDN endpoints this skill uses, discovered via browser session analysis of
ikea.com/us/en. The `scripts/` in this skill implement the common path; this doc is the full map for edge cases.

**Verified June 2026 (US market): the whole download path needs NO cookies.** The only cookie-gated endpoint is the
metadata JSON (§4), which the skill avoids.

| Endpoint                         | Cookies?                                  | Used for                             |
| -------------------------------- | ----------------------------------------- | ------------------------------------ |
| `sik...cdtapps.com` search (§2a) | no                                        | name → item number (structured JSON) |
| product page HTML (§2b)          | no                                        | color variants, dimensions           |
| rotera static `-mini.glb` (§3)   | no                                        | the actual model download            |
| rotera metadata JSON (§4)        | **yes (401 without)**                     | optional; skill does not use it      |
| dimma higher-detail glb (§5)     | no (but URL must be scraped from page JS) | optional higher-quality model        |

Origins:

- Product pages: `https://www.ikea.com/us/en/`
- Search backend: `https://sik.search.blue.cdtapps.com/us/en/`
- Model / API layer: `https://web-api.ikea.com`

---

## 1. Finding products & item numbers

### 2a. Structured search API (preferred — `search_ikea.py` uses this)

```
GET https://sik.search.blue.cdtapps.com/{market}/{lang}/search-result-page
      ?q={query}&types=PRODUCT&size={n}&c=sr&v=20240110
```

Returns JSON. Products live at `searchResultPage.products.main.items[].product`, each with:

- `itemNoGlobal` — the 8-digit item number (use this)
- `name`, `typeName` — e.g. "BESTÅ", "TV unit"
- `itemMeasureReferenceText` — human dimensions, e.g. `70 7/8x15 3/4x25 1/4 "`

No cookies. This is the reliable way to get item numbers — far better than scraping the search HTML page (which is a
JS-rendered shell and does not reliably contain product links).

### 2b. Direct product page navigation

```
https://www.ikea.com/us/en/p/{product-slug}-{8-digit-item-number}/
```

e.g. `.../p/besta-tv-unit-black-brown-00566036/`. Combination/assembled products may carry an `s` prefix in the slug
(`s19272703`); the API item number is the digits only (`19272703`). Public, no cookies.

### 2c. Color variants (structured — no scraping)

The §2a `sik` response already carries every color variant per product under `gprDescription`:

```json
"gprDescription": {
  "numberOfVariants": 13,
  "variants": [
    {"id": "40575227", "pipUrl": ".../p/besta-tv-unit-dark-gray-40575227/"},
    {"id": "80566037", "pipUrl": ".../p/besta-tv-unit-white-80566037/"}
  ]
}
```

`id` is the variant's item number; the color is the `pipUrl` slug with the shared product-type prefix removed.
`search_ikea.py --variants` does exactly this. Combination products give an `s`-prefixed id (`s49568048`) — strip the
`s` for the model URL (`49568048-mini.glb` is 200, `s49568048-mini.glb` is 400); `search_ikea.py` strips it for you.

Use this when the wanted color returns 404 — pick another variant's item number. Verified: `40575227`→200,
`80566037`→200, `00565777`→404, `20576001`→404.

> Fallback only: the same variant links also appear as `<a href="/us/en/p/...">` anchors in the product-page HTML, if
> you ever need them without the search API.

### 2d. Art number → item number

The label "art number" is the item number with dots removed: `005.660.36 → 00566036`, `103.332.87 → 10333287`.

---

## 3. Downloading the GLB (rotera static — primary, no auth)

```
GET https://web-api.ikea.com/{market}/{lang}/rotera/static/models/{itemNumber}-mini.glb
```

- `Content-Type: model/gltf-binary`; binary GLB (glTF 2.0, **Draco-compressed**)
- `404` if no 3D model exists for that item (discontinued / never scanned)
- `400` for invalid URL variants (e.g. dropping the `-mini` suffix)
- HEAD-probe before downloading to distinguish 200 / 404 (the download script does this).

The `-mini` suffix is the simplified model served to the browser 3D viewer — a complete, valid GLB with reduced
geometry. There is no reliable non-`-mini` rotera variant.

---

## 4. Metadata JSON (rotera) — OPTIONAL, cookie-gated, skill avoids it

```
GET https://web-api.ikea.com/{market}/{lang}/rotera/data/model/{itemNumber}/
```

Returns `productName`, `measurements` (mm), and `modelUrl`. **Returns 401 without a session cookie** (the cookie is set
when the "View in 3D" button is clicked on a product page in a real browser). The skill does not need this: the model
URL is constructible (§3) and dimensions come from search (§2a) or the converted model's bounding box. Documented only
for completeness.

---

## 5. Higher-detail "dimma" models — OPTIONAL, the one remaining scrape

> First check rotera (§3): it usually exists and avoids this path entirely. The LILLÅNÄS chair (item `00534757`) — once
> thought dimma-only — **does have a rotera `-mini` model (verified 200)**. Only reach for dimma when you specifically
> need the higher-detail mesh.

This is the only data the skill cannot get structurally: the URL embeds a content hash + revision that exist **only in
the rendered product page's JavaScript** — there is no queryable/guessable endpoint. It must be scraped.

Some products have a higher-detail model served via the `dimma`/geomagical pipeline:

```
https://web-api.ikea.com/dimma/assets/geomagical/{itemNumber}/{variantCode}/
    simple/glb_draco/{hash}-G-{itemNumber}-{revision}-simple+draco.glb?cn=pip
```

The URL contains a content hash + revision you **cannot guess** — extract it from the rendered product page's
JavaScript:

```javascript
const allJS = Array.from(document.querySelectorAll("script"))
  .map((s) => s.textContent)
  .join("\n");
console.log(allJS.match(/https?:\/\/[^\s"'`]+\.glb[^\s"'`]*/gi) || []);
```

No cookies once you have the full URL. Also Draco-compressed, so it goes through the same `glb_to_stl.sh` convert step.
Larger/higher-quality than rotera `-mini` (~448 KB vs ~274 KB for the examples below).

---

## 6. Known working examples (US market, verified June 2026)

| Product                          | Item No  | Source                                     | Size   |
| -------------------------------- | -------- | ------------------------------------------ | ------ |
| BESTÅ TV unit, black-brown       | 00566036 | rotera `-mini`                             | 274 KB |
| BESTÅ TV unit, white             | 80566037 | rotera `-mini`                             | 286 KB |
| BROR shelving unit, black/wood   | 19272703 | rotera `-mini`                             | 665 KB |
| LILLÅNÄS chair, chrome/dark gray | 80534758 | rotera `-mini` (also dimma, higher-detail) | 448 KB |

---

## 7. Notes & caveats

- **Market-specific:** URLs use `/us/en/`. Other markets use `/gb/en/`, `/de/de/`, etc.; item numbers can differ per
  market.
- **Discontinued products** return 404 on both the product page and the model endpoint.
- **Format:** standard GLB (glTF binary 2.0), Draco-compressed. Opens in Blender (File → Import → glTF 2.0), three.js,
  Babylon.js. For FreeCAD it must be Draco-decoded first — that is what this skill's convert step does.
- **Be polite** in batch downloads: a 500–1000 ms delay between requests. No rate limiting was observed, but don't
  hammer it.
