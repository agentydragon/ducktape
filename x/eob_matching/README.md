# EOB Payment Matching

Match Anthem bank deposits to their constituent medical/pharmacy claims and providers.

## Status

Actual matching algorithm is sketched out but mid-WIP. Answer to question that needed answering was
found just with data extraction from PDFs without needing full matching.

## Layout

```text
x/eob_matching/
├── BUILD.bazel
├── models.py              # Pydantic models: Claim, EOB, BankPayment, extraction schemas
├── loaders.py             # Data loaders for CSV, JSON, bank statement
├── matcher.py             # DP subset-sum matching
├── main.py                # Main binary: match payments → payment_results.json
├── parse_eob_listing.py   # Parse EOB Center HTML → eob_listing.json
├── extract_pdfs.py        # Vision model extraction: page-1 summary, page-3+ claims
└── output/                # Generated data (gitignored)
    ├── eob_listing.json
    ├── eob_extractions.json
    └── payment_results.json
```

## Bazel targets

| Target                                   | What                                                                                  |
| ---------------------------------------- | ------------------------------------------------------------------------------------- |
| `//x/eob_matching:parse_eob_listing_bin` | Parse HTML → `output/eob_listing.json`                                                |
| `//x/eob_matching:extract_pdfs_bin`      | Vision model PDF extraction → `output/eob_extractions.json` (needs ollama + pdftoppm) |
| `//x/eob_matching:eob_matching`          | Match payments → `output/payment_results.json`                                        |

## Data sources

| Source           | Location                                                                                                |
| ---------------- | ------------------------------------------------------------------------------------------------------- |
| Claims CSV       | `~/downloads/anthem-claims-2024-04-01-through-2026-04-01.csv` — 218 claims (112 medical + 106 pharmacy) |
| Bank statement   | `~/downloads/Bank of America statements 2024-10-01 through 2026-04-01.txt` — 32 Anthem EFT deposits     |
| EOB listing HTML | `~/downloads/anthem-eobs/EOB Center Medical.html` — 84 medical EOBs                                     |
| EOB PDFs         | `~/downloads/anthem-eobs/*.pdf` — 83 unique (deduped from 98)                                           |
| Ollama models    | `~/downloads/ollama-models/` — qwen2.5vl:32b                                                            |
| OCR cache        | `~/downloads/eob-cache/` — cached per-PDF extraction results                                            |
