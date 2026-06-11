"""Mini-eval for EOB PDF extraction quality.

Every (PDF, run, field) tuple is an independent accuracy sample.
Accuracy is measured across ALL runs. Per-field and per-PDF breakdowns reported.

Requires: ollama running with qwen2.5vl model, pdftoppm on PATH.
"""

import sys
import tempfile
from collections import defaultdict
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from x.eob_matching.extract_pdfs import CLAIMS_PROMPT, SUMMARY_PROMPT, query_ollama
from x.eob_matching.models import EOBClaimsPageExtraction, EOBSummaryExtraction, ExtractedDate
from x.eob_matching.pdf_utils import render_pdf_page

EOB_DIR = Path.home() / "downloads" / "anthem-eobs"
RUNS_PER_PAGE = 3
TOLERANCE_DOLLARS = 0.02


# --- Ground truth ---


class SummaryGT(BaseModel):
    statement_date: date
    doctor_facility_charges: float = Field(ge=0)
    your_discounts: float = Field(le=0)
    allowed_amount: float = Field(ge=0)
    anthem_blue_cross_paid: float = Field(le=0)
    what_you_pay: float = Field(ge=0)


class ClaimLineGT(BaseModel):
    service_date: date
    service_description: str
    doctor_charges: float
    anthem_blue_cross_paid: float
    your_total_cost: float
    reason_code: str = ""


class ClaimGT(BaseModel):
    claim_number: str
    received_date: date
    doctor_name: str
    in_network: bool
    you_pay_total: float
    lines: list[ClaimLineGT]


class DetailPageGT(BaseModel):
    page: int
    claims: list[ClaimGT]


class PDFGT(BaseModel):
    """All ground truth for one PDF."""

    pdf_stem: str
    summary: SummaryGT
    detail_pages: list[DetailPageGT] = []


GROUND_TRUTHS = [
    PDFGT(
        pdf_stem="4de141d0",
        summary=SummaryGT(
            statement_date=date(2025, 12, 19),
            doctor_facility_charges=8090.88,
            your_discounts=0.0,
            allowed_amount=4777.89,
            anthem_blue_cross_paid=-4757.89,
            what_you_pay=3332.99,
        ),
    ),
    PDFGT(
        pdf_stem="2259ae70",
        summary=SummaryGT(
            statement_date=date(2025, 4, 27),
            doctor_facility_charges=4401.54,
            your_discounts=-2373.67,
            allowed_amount=2027.87,
            anthem_blue_cross_paid=-2007.87,
            what_you_pay=20.00,
        ),
        detail_pages=[
            DetailPageGT(
                page=3,
                claims=[
                    ClaimGT(
                        claim_number="2025114EQ6363",
                        received_date=date(2025, 4, 24),
                        doctor_name="MCINNES, LYNNE A",
                        in_network=True,
                        you_pay_total=20.00,
                        lines=[
                            ClaimLineGT(
                                service_date=date(2025, 3, 27),
                                service_description="Medical Service",
                                doctor_charges=4401.54,
                                anthem_blue_cross_paid=2007.87,
                                your_total_cost=20.00,
                                reason_code="066",
                            )
                        ],
                    )
                ],
            )
        ],
    ),
    PDFGT(
        pdf_stem="e9ea0273",
        summary=SummaryGT(
            statement_date=date(2025, 8, 14),
            doctor_facility_charges=1200.00,
            your_discounts=0.0,
            allowed_amount=1200.00,
            anthem_blue_cross_paid=-840.00,
            what_you_pay=360.00,
        ),
        detail_pages=[
            DetailPageGT(
                page=3,
                claims=[
                    ClaimGT(
                        claim_number="2025221RM1565",
                        received_date=date(2025, 8, 9),
                        doctor_name="LESNE",
                        in_network=False,
                        you_pay_total=360.00,
                        lines=[
                            ClaimLineGT(
                                service_date=date(2025, 6, d),
                                service_description="Therapeutic Services",
                                doctor_charges=200.00,
                                anthem_blue_cross_paid=140.00,
                                your_total_cost=60.00,
                            )
                            for d in [6, 9, 13, 20, 23, 27]
                        ],
                    )
                ],
            )
        ],
    ),
    PDFGT(
        pdf_stem="274ddb79",
        summary=SummaryGT(
            statement_date=date(2026, 3, 19),
            doctor_facility_charges=71580.50,
            your_discounts=0.0,
            allowed_amount=71580.50,
            anthem_blue_cross_paid=-71580.50,
            what_you_pay=0.00,
        ),
        detail_pages=[
            DetailPageGT(
                page=3,
                claims=[
                    ClaimGT(
                        claim_number="2025217BP9584",
                        received_date=date(2025, 8, 5),
                        doctor_name="NUMA PSYCHIATRY & PSYCHED",
                        in_network=False,
                        you_pay_total=0.00,
                        lines=[
                            ClaimLineGT(
                                service_date=date(2025, 7, 31),
                                service_description="Drug Non-Oral",
                                doctor_charges=14316.10,
                                anthem_blue_cross_paid=14316.10,
                                your_total_cost=0.00,
                            )
                        ],
                    ),
                    ClaimGT(
                        claim_number="2025224BA6430",
                        received_date=date(2025, 8, 12),
                        doctor_name="NUMA PSYCHIATRY & PSYCHED",
                        in_network=False,
                        you_pay_total=0.00,
                        lines=[
                            ClaimLineGT(
                                service_date=date(2025, 8, 7),
                                service_description="Drug Non-Oral",
                                doctor_charges=14316.10,
                                anthem_blue_cross_paid=14316.10,
                                your_total_cost=0.00,
                            )
                        ],
                    ),
                ],
            )
        ],
    ),
]


# --- Eval machinery ---


class CheckResult(BaseModel):
    pdf_stem: str
    run: int
    field: str
    passed: bool
    expected: str
    actual: str


class EvalResults:
    def __init__(self) -> None:
        self.checks: list[CheckResult] = []
        self.extraction_failures: list[Exception] = []

    def _add(self, pdf_stem: str, run: int, field: str, passed: bool, expected: str, actual: str) -> None:
        self.checks.append(
            CheckResult(pdf_stem=pdf_stem, run=run, field=field, passed=passed, expected=expected, actual=actual)
        )

    def check_value(self, pdf_stem: str, run: int, field: str, expected: float, actual: float) -> None:
        self._add(pdf_stem, run, field, abs(expected - actual) <= TOLERANCE_DOLLARS, str(expected), str(actual))

    def check_date(self, pdf_stem: str, run: int, field: str, expected: date, actual: ExtractedDate) -> None:
        self._add(pdf_stem, run, field, actual.to_date() == expected, str(expected), str(actual.to_date()))

    def check_str(self, pdf_stem: str, run: int, field: str, expected: str, actual: str) -> None:
        self._add(pdf_stem, run, field, expected.lower() in actual.lower(), expected, actual)

    def check_bool(self, pdf_stem: str, run: int, field: str, expected: bool, actual: bool) -> None:
        self._add(pdf_stem, run, field, expected == actual, str(expected), str(actual))


# --- Extraction ---


def find_pdf(stem: str) -> Path:
    matches = list(EOB_DIR.glob(f"{stem}*.pdf"))
    if len(matches) != 1:
        raise FileNotFoundError(f"Expected 1 PDF matching '{stem}*', found {len(matches)}")
    return matches[0]


def extract_page_n_times[T: BaseModel](
    pdf_path: Path, page: int, prompt: str, response_model: type[T], n: int
) -> tuple[list[tuple[int, T]], list[tuple[int, Exception]]]:
    """Extract a page N times. Returns (successes, failures) with run indices."""
    successes: list[tuple[int, T]] = []
    failures: list[tuple[int, Exception]] = []
    for run in range(n):
        with tempfile.TemporaryDirectory() as tmpdir:
            img = render_pdf_page(pdf_path, page=page, tmpdir=Path(tmpdir))
            try:
                successes.append((run, query_ollama(img, prompt, response_model)))
            except (ValidationError, Exception) as e:
                failures.append((run, e))
    return successes, failures


# --- Eval per PDF ---


def eval_pdf(gt: PDFGT, results: EvalResults) -> None:
    """Evaluate one PDF: summary page + all detail pages, N runs each."""
    pdf_path = find_pdf(gt.pdf_stem)

    # Summary (page 1)
    successes, failures = extract_page_n_times(
        pdf_path, page=1, prompt=SUMMARY_PROMPT, response_model=EOBSummaryExtraction, n=RUNS_PER_PAGE
    )
    results.extraction_failures.extend(exc for _, exc in failures)

    for run, e in successes:
        results.check_date(gt.pdf_stem, run, "statement_date", gt.summary.statement_date, e.statement_date)
        results.check_value(
            gt.pdf_stem, run, "doctor_facility_charges", gt.summary.doctor_facility_charges, e.doctor_facility_charges
        )
        results.check_value(gt.pdf_stem, run, "your_discounts", gt.summary.your_discounts, e.your_discounts)
        results.check_value(gt.pdf_stem, run, "allowed_amount", gt.summary.allowed_amount, e.allowed_amount)
        results.check_value(
            gt.pdf_stem, run, "anthem_blue_cross_paid", gt.summary.anthem_blue_cross_paid, e.anthem_blue_cross_paid
        )
        results.check_value(gt.pdf_stem, run, "what_you_pay", gt.summary.what_you_pay, e.what_you_pay)

    # Detail pages
    for detail_gt in gt.detail_pages:
        detail_successes, detail_failures = extract_page_n_times(
            pdf_path, page=detail_gt.page, prompt=CLAIMS_PROMPT, response_model=EOBClaimsPageExtraction, n=RUNS_PER_PAGE
        )
        results.extraction_failures.extend(exc for _, exc in detail_failures)

        for run, detail_e in detail_successes:
            if len(detail_e.claims) != len(detail_gt.claims):
                results._add(
                    gt.pdf_stem, run, "claims_length", False, str(len(detail_gt.claims)), str(len(detail_e.claims))
                )
                continue

            for gt_claim, ext_claim in zip(detail_gt.claims, detail_e.claims, strict=True):
                results.check_str(gt.pdf_stem, run, "claim_number", gt_claim.claim_number, ext_claim.claim_number)
                results.check_date(gt.pdf_stem, run, "received_date", gt_claim.received_date, ext_claim.received_date)
                results.check_str(gt.pdf_stem, run, "doctor_name", gt_claim.doctor_name, ext_claim.doctor_name)
                results.check_bool(gt.pdf_stem, run, "in_network", gt_claim.in_network, ext_claim.in_network)
                results.check_value(gt.pdf_stem, run, "you_pay_total", gt_claim.you_pay_total, ext_claim.you_pay_total)

                if len(ext_claim.lines) != len(gt_claim.lines):
                    results._add(
                        gt.pdf_stem,
                        run,
                        f"lines_length[{gt_claim.claim_number}]",
                        False,
                        str(len(gt_claim.lines)),
                        str(len(ext_claim.lines)),
                    )
                    continue

                for gt_line, ext_line in zip(gt_claim.lines, ext_claim.lines, strict=True):
                    results.check_date(gt.pdf_stem, run, "service_date", gt_line.service_date, ext_line.service_date)
                    results.check_value(
                        gt.pdf_stem, run, "doctor_charges", gt_line.doctor_charges, ext_line.doctor_charges
                    )
                    results.check_value(
                        gt.pdf_stem, run, "anthem_paid", gt_line.anthem_blue_cross_paid, ext_line.anthem_blue_cross_paid
                    )
                    results.check_value(
                        gt.pdf_stem, run, "your_total_cost", gt_line.your_total_cost, ext_line.your_total_cost
                    )


# --- Reporting (display only) ---


def print_results(results: EvalResults) -> None:
    checks = results.checks
    total = len(checks)
    passed = sum(1 for c in checks if c.passed)
    accuracy = passed / total if total else 0

    print(f"\n{'=' * 60}", file=sys.stderr)
    print(f"OVERALL: {passed}/{total} ({accuracy:.1%})", file=sys.stderr)
    print(f"Extraction failures: {len(results.extraction_failures)}", file=sys.stderr)

    by_field: dict[str, list[CheckResult]] = defaultdict(list)
    for c in checks:
        by_field[c.field].append(c)
    print("\n  Per-field accuracy:", file=sys.stderr)
    for field in sorted(by_field):
        fc = by_field[field]
        fp = sum(1 for c in fc if c.passed)
        print(f"    {field:30s}  {fp}/{len(fc)} ({fp / len(fc):.0%})", file=sys.stderr)

    by_pdf: dict[str, list[CheckResult]] = defaultdict(list)
    for c in checks:
        by_pdf[c.pdf_stem].append(c)
    print("\n  Per-PDF accuracy:", file=sys.stderr)
    for pdf in sorted(by_pdf):
        pc = by_pdf[pdf]
        pp = sum(1 for c in pc if c.passed)
        print(f"    {pdf:30s}  {pp}/{len(pc)} ({pp / len(pc):.0%})", file=sys.stderr)

    errors = [c for c in checks if not c.passed]
    if errors:
        print(f"\n  Errors ({len(errors)}):", file=sys.stderr)
        for c in errors:
            print(f"    {c.pdf_stem} run {c.run + 1} {c.field}: expected={c.expected} got={c.actual}", file=sys.stderr)

    for exc in results.extraction_failures:
        print(f"  Extraction failure: {exc}", file=sys.stderr)


# --- Main ---


def main() -> None:
    results = EvalResults()

    for gt in GROUND_TRUTHS:
        eval_pdf(gt, results)

    print_results(results)

    total = len(results.checks)
    passed = sum(1 for c in results.checks if c.passed)
    accuracy = passed / total if total else 0

    if results.extraction_failures:
        print(f"\nFAIL: {len(results.extraction_failures)} extraction failures", file=sys.stderr)
        sys.exit(1)
    elif accuracy < 0.9:
        print(f"\nFAIL: accuracy {accuracy:.0%} < 90%", file=sys.stderr)
        sys.exit(1)
    else:
        print("\nPASS", file=sys.stderr)


if __name__ == "__main__":
    main()
