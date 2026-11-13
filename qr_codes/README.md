# QR Codes

SVG QR codes for places around the house.

## Generating

Requires Bebas Neue installed as a system font (via home-manager on wyrm2).
Bazel runs the generator from its execroot, so use `$PWD` to write into the directory where you invoked it.

```bash
bb run //qr_codes:gen_bin -- \
  --text 'TEXT_TO_ENCODE' \
  --caption 'Caption below code' \
  --output "$PWD/output.svg"
```

## Codes

| Text                     | Caption           |
| ------------------------ | ----------------- |
| `Morning alarm complete` | Rai morning alarm |
