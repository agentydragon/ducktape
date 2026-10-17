"""Parse EOB Center Medical.html to extract claim-to-EOB mapping.

Outputs eob_listing.json with all EOBs and their claim groupings.
"""

import json
import re
import sys
from pathlib import Path

from more_itertools import unique_everseen

try:
    from bs4 import BeautifulSoup

    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

HTML_PATH = Path.home() / "downloads" / "anthem-eobs" / "EOB Center Medical.html"
OUTPUT_DIR = Path.home() / "code" / "ducktape" / "x" / "eob_matching" / "output"


def parse_from_html(path: Path) -> list[dict]:
    """Parse structured HTML for EOB extraction."""
    with path.open() as f:
        soup = BeautifulSoup(f, "html.parser")

    text = soup.get_text(separator="\n")
    blocks = re.split(r"(?=EOB Statement Date[:\s])", text)
    blocks = [b for b in blocks if re.match(r"EOB Statement Date", b)]

    eobs: list[dict] = []
    for block in blocks:
        date_m = re.search(r"EOB Statement Date[:\s]*(\d{2}/\d{2}/\d{4})", block)
        svc_m = re.search(r"Service Dates[:\s]*([\d/]+ - [\d/]+|\d{2}/\d{2}/\d{4})", block)

        claim_nums = re.findall(r"\b(20\d{2}[0-9A-Z]{5,})\b", block)
        unique_claims = list(unique_everseen(claim_nums))

        provider = ""
        if "Care Provider" in block:
            after_provider = block.split("Care Provider")[-1]
            for line in after_provider.split("\n"):
                stripped = line.strip()
                if (
                    stripped
                    and not re.match(r"^[\$\d,.\s-]+$", stripped)
                    and not re.match(r"^\d{2}/\d{2}", stripped)
                    and len(stripped) > 2
                ):
                    provider = stripped
                    break

        your_costs: list[str] = []
        if "Amount You Pay" in block:
            after_amount = block.split("Amount You Pay")[-1][:500]
            your_costs = re.findall(r"\$[\d,.]+", after_amount)

        if date_m:
            eobs.append(
                {
                    "statement_date": date_m.group(1),
                    "service_dates": svc_m.group(1) if svc_m else "",
                    "claim_nums": unique_claims,
                    "provider": provider,
                    "your_costs": your_costs,
                }
            )

    return eobs


def main() -> None:
    if not HAS_BS4:
        print("beautifulsoup4 required. Run in nix-shell or bazel.", file=sys.stderr)
        sys.exit(1)

    if not HTML_PATH.exists():
        print(f"HTML file not found: {HTML_PATH}", file=sys.stderr)
        sys.exit(1)

    print(f"Parsing: {HTML_PATH}", file=sys.stderr)
    eobs = parse_from_html(HTML_PATH)

    # Deduplicate by (statement_date, claim set)
    unique_eobs = list(unique_everseen(eobs, key=lambda e: (e["statement_date"], frozenset(e["claim_nums"]))))

    all_claims = {cn for e in unique_eobs for cn in e["claim_nums"]}
    print(
        f"Parsed {len(eobs)} blocks, {len(unique_eobs)} unique EOBs, {len(all_claims)} unique claims", file=sys.stderr
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / "eob_listing.json"
    output_path.write_text(json.dumps(unique_eobs, indent=2))
    print(f"Wrote {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
