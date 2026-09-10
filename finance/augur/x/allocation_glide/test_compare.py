"""Exercise the documented Python CLI, compact populations and selected financial replay."""

import json
import subprocess
from pathlib import Path

import pytest
import pytest_bazel

from finance.augur.x.allocation_glide.compare import execute
from util.bazel.runfiles import get_required_path, own_repo_rlocation


@pytest.fixture(scope="module")
def output(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("allocation-glide") / "results"
    subprocess.run(
        [get_required_path(own_repo_rlocation("finance/augur/x/allocation_glide/compare_bin")), "--output-dir", output],
        check=True,
    )
    return output


def test_constant_and_glide_have_funded_consumption_and_distinct_holdings(output: Path) -> None:
    constant = json.loads((output / "constant.json").read_text())["rollouts"]
    glide = json.loads((output / "glide.json").read_text())["rollouts"]
    for fixed, varying in zip(constant, glide, strict=True):
        assert fixed["stop"] is varying["stop"] is None
        assert fixed["trace"] is varying["trace"] is None
        assert fixed["summary"]["tax_accruals"] == varying["summary"]["tax_accruals"] == []
        fixed_paid = [
            (row["month"], row["receipt"]["amount_requested"])
            for row in fixed["summary"]["payments"]
            if row["receipt"]["outcome"] == "Paid"
        ]
        varying_paid = [
            (row["month"], row["receipt"]["amount_requested"])
            for row in varying["summary"]["payments"]
            if row["receipt"]["outcome"] == "Paid"
        ]
        assert fixed_paid == varying_paid
        assert fixed_paid[:2] == [(0, 600_000), (12, 615_000)]
        assert fixed["summary"]["cash"][0]["values"][:13] == varying["summary"]["cash"][0]["values"][:13]
        assert fixed["summary"]["ending_book"]["lots"] != varying["summary"]["ending_book"]["lots"]


@pytest.mark.parametrize("name", ["constant", "glide"])
def test_selected_replay_matches_compact_population_and_keeps_balanced_books(output: Path, name: str) -> None:
    population = json.loads((output / f"{name}.json").read_text())["rollouts"]
    traces = json.loads((output / f"{name}-traces.json").read_text())["rollouts"]
    assert [row["rollout_id"] for row in traces] == [2, 0]
    for row in traces:
        assert row["summary"] == population[row["rollout_id"]]["summary"]
        financial = row["trace"]["financial"]
        assert row["summary"]["ending_book"] == financial["months"][-1]
        assert financial["dispositions"]
        for entry in financial["journal"]:
            assert sum(posting["amount"] for posting in entry["postings"]) == 0
    [replay] = execute(
        (output / "execution-input.json").read_text(),
        annual_step=0 if name == "constant" else 5,
        rollout_ids=[2],
        capture="forensic",
    )
    assert replay == traces[0]


if __name__ == "__main__":
    pytest_bazel.main()
