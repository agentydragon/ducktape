"""Exercise prepared paths -> native target functions -> canonical funding and holdings."""

import json
from pathlib import Path

import pytest_bazel

from finance.augur.x.allocation_glide.compare import compare


def test_constant_and_glide_have_funded_consumption_and_distinct_holdings(tmp_path: Path) -> None:
    output = tmp_path / "test-comparison"
    compare(output)
    constant = json.loads((output / "constant.json").read_text())
    glide = json.loads((output / "glide.json").read_text())
    for fixed, varying in zip(constant["rollouts"], glide["rollouts"], strict=True):
        assert fixed["failed_month"] is None
        assert varying["failed_month"] is None
        assert fixed["tax_accruals"] == varying["tax_accruals"] == []
        fixed_paid = [
            (row["month"], row["amount_paid"]) for row in fixed["obligations"] if row["obligation_type"] == "cash_spend"
        ]
        varying_paid = [
            (row["month"], row["amount_paid"])
            for row in varying["obligations"]
            if row["obligation_type"] == "cash_spend"
        ]
        assert fixed_paid == varying_paid
        assert fixed_paid[0] == (0, 600_000)
        assert fixed_paid[1] == (12, 615_000)
        assert fixed["dispositions"]
        assert varying["dispositions"]
        assert fixed["months"][:13] == varying["months"][:13]
        assert fixed["months"][-1]["lots"] != varying["months"][-1]["lots"]


if __name__ == "__main__":
    pytest_bazel.main()
