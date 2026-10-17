"""Extract structured data from EOB PDFs using Qwen2.5-VL via ollama.

For each PDF, extracts:
- Page 1: financial summary (statement date, amounts)
- Pages 3+: claims detail (claim numbers, providers, service lines)

Caches complete results per PDF content hash.

Requires:
- ollama running with qwen2.5vl model (with CUDA)
- poppler-utils (pdftoppm, pdfinfo) on PATH
"""

import base64
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
from more_itertools import unique_everseen
from pydantic import BaseModel

from x.eob_matching.models import EOBClaimsPageExtraction, EOBSummaryExtraction, PDFExtraction
from x.eob_matching.pdf_utils import file_hash, render_pdf_page

EOB_DIR = Path.home() / "downloads" / "anthem-eobs"
CACHE_DIR = Path.home() / "downloads" / "eob-cache"
OUTPUT_PATH = Path.home() / "code" / "ducktape" / "x" / "eob_matching" / "output" / "eob_extractions.json"

OLLAMA_URL = "http://localhost:11434"
MODEL = "qwen2.5vl:32b"

SUMMARY_PROMPT = (
    "This is page 1 of an Anthem Blue Cross EOB (Explanation of Benefits).\n"
    "\n"
    'The statement date is in the top-right area, under the bold "Health Care Summary" heading, '
    'above the paragraph starting "Also called an Explanation of Benefits".\n'
    "\n"
    'The "Claims summary" is in a box with a light green border in the lower-left of the page. '
    "It has 4 lines with dollar amounts, stacked vertically:\n"
    '1. "Doctor/facility charges:" — a positive dollar amount\n'
    '2. "Your discounts:" — a negative dollar amount (or -0.00)\n'
    '3. "Due to your doctor/facility (max allowed):" — a positive dollar amount\n'
    '4. "Anthem Blue Cross paid:" — ALWAYS a negative number. This is the LAST line '
    'before "What you may pay". It is a DIFFERENT value than '
    '"Due to your doctor/facility (max allowed)" on the line above it.\n'
    "\n"
    'Below the box: "What you may pay:" in a colored banner — a positive dollar amount.\n'
    "\n"
    'IMPORTANT: "Anthem Blue Cross paid" and "Due to your doctor/facility (max allowed)" '
    "are two different lines with different values. "
    "Read the exact digits on EACH line separately. anthem_blue_cross_paid must be negative or zero."
)

CLAIMS_PROMPT = (
    "This is a claims detail page from an Anthem Blue Cross EOB.\n"
    "\n"
    "Each claim block has:\n"
    '- "Claim Number:" followed by an alphanumeric code (e.g. 2025347KX8291)\n'
    '- "Received:" followed by a date in MM/DD/YY format (e.g. 08/09/25)\n'
    '- "Doctor:" followed by the provider name\n'
    "- A table of service lines with columns: Service date (MM/DD/YY format), Service, "
    "Reason code, Doctor charges, Your discounts, Due to your doctor (max allowed), "
    "Anthem Blue Cross paid, Copay, Deductible, Your share of the cost (coinsurance), "
    "Services not covered, Your total cost\n"
    "\n"
    "Extract ALL claims and ALL service lines from this page."
)


def query_ollama[T: BaseModel](image_path: Path, prompt: str, response_model: type[T]) -> T:
    """Send image to ollama vision model with structured JSON output.

    The JSON schema is both:
    1. Passed to ollama's `format` param for grammar-based structural enforcement
    2. Included in the prompt text so the model sees field names, descriptions, and constraints
    """
    b64 = base64.b64encode(image_path.read_bytes()).decode()
    schema = response_model.model_json_schema()
    full_prompt = f"{prompt}\n\nOutput JSON matching this schema:\n{json.dumps(schema, indent=2)}"

    resp = httpx.post(
        f"{OLLAMA_URL}/api/generate",
        json={"model": MODEL, "prompt": full_prompt, "images": [b64], "format": schema, "stream": False},
        timeout=120,
    )
    resp.raise_for_status()
    return response_model.model_validate_json(resp.json()["response"])


def _get_page_count(pdf_path: Path) -> int:
    result = subprocess.run(["pdfinfo", str(pdf_path)], capture_output=True, text=True, check=True)
    for line in result.stdout.split("\n"):
        if "Pages:" in line:
            return int(line.split(":")[1].strip())
    raise RuntimeError(f"Could not determine page count for {pdf_path}")


def extract_pdf(pdf_path: Path) -> PDFExtraction:
    """Extract summary + all claims from one PDF."""
    pages = _get_page_count(pdf_path)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # Page 1: summary
        img = render_pdf_page(pdf_path, page=1, tmpdir=tmp)
        summary = query_ollama(img, SUMMARY_PROMPT, EOBSummaryExtraction)

        # Pages 3+: claims detail (page 2 is year-to-date boilerplate)
        all_claims = []
        for page in range(3, pages + 1):
            img = render_pdf_page(pdf_path, page=page, tmpdir=tmp)
            page_extraction = query_ollama(img, CLAIMS_PROMPT, EOBClaimsPageExtraction)
            all_claims.extend(page_extraction.claims)

    return PDFExtraction(pdf=pdf_path.name, summary=summary, claims=all_claims)


def process_pdf(pdf_path: Path) -> tuple[PDFExtraction | None, str]:
    """Extract with caching. Returns (extraction, status)."""
    h = file_hash(pdf_path)
    cache_path = CACHE_DIR / f"{h}.json"

    if cache_path.exists():
        return PDFExtraction.model_validate_json(cache_path.read_text()), "cached"

    try:
        extraction = extract_pdf(pdf_path)
    except Exception as e:
        return None, f"error: {e}"

    cache_path.write_text(extraction.model_dump_json(indent=2))
    return extraction, "extracted"


def main() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    pdfs = sorted(EOB_DIR.glob("*.pdf"))
    print(f"Found {len(pdfs)} PDFs", file=sys.stderr)

    # Deduplicate by content hash
    unique = list(unique_everseen(pdfs, key=file_hash))
    print(f"Unique: {len(unique)}", file=sys.stderr)

    results: list[PDFExtraction] = []
    cached = extracted = errors = 0

    for i, pdf_path in enumerate(unique):
        extraction, status = process_pdf(pdf_path)
        if status == "cached":
            cached += 1
        elif extraction is not None:
            extracted += 1
        else:
            errors += 1

        if extraction is not None:
            results.append(extraction)
            n_claims = len(extraction.claims)
            print(
                f"[{i + 1}/{len(unique)}] {extraction.pdf[:35]:>35}  "
                f"paid={extraction.summary.anthem_blue_cross_paid:>12,.2f}  "
                f"claims={n_claims}  [{status}]",
                file=sys.stderr,
            )
        else:
            print(f"[{i + 1}/{len(unique)}] {pdf_path.name[:35]:>35}  FAILED: {status}", file=sys.stderr)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps([r.model_dump(mode="json") for r in results], indent=2))
    print(f"\nDone: {cached} cached, {extracted} extracted, {errors} errors", file=sys.stderr)
    print(f"Wrote {len(results)} to {OUTPUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
