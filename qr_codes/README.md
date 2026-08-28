# QR Codes

SVG QR codes for places around the house.

## Generating

Requires Bebas Neue installed as a system font (via home-manager on wyrm2).

```bash
bazel run //qr_codes:gen_bin -- \
  --text 'TEXT_TO_ENCODE' \
  --caption 'Caption below code' \
  --output path/to/output.svg
```

## Codes

| Text                     | Caption           |
| ------------------------ | ----------------- |
| `Morning alarm complete` | Rai morning alarm |
