from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import pytest
import pytest_bazel
import yaml

# Aliased to avoid shadowing the builtin.
from inspect_ai import eval as inspect_eval
from inspect_ai.model import ModelOutput, get_model
from more_itertools import one

from loom.gym.dossier import series_dossier
from loom.gym.inspect_harness import COMPOSE_PATH, agent_eval_task, sample_for_task
from loom.gym.monthly_series import MonthlySeries, add_months
from loom.gym.series_tasks import SeriesTaskSpec, tasks_for_spec
from loom.gym.task import BinaryOutcome

# Linear ramp over 2020-01..2020-12: from anchor 2020-01 (level 100) the ramp
# reaches 112 by +6m, crossing the 1.05x threshold (105) → outcome YES.
RAMP = MonthlySeries(
    series_id="ramp",
    description="test ramp",
    unit="units",
    provenance="synthetic",
    values={add_months(date(2020, 1, 1), n): 100.0 + 2 * n for n in range(12)},
)

GYM_TASK = one(
    tasks_for_spec(
        SeriesTaskSpec(series=RAMP, binary_thresholds=((6, 1.05),), scalar_horizons=()),
        anchor_start=date(2020, 1, 1),
        anchor_step_months=12,
    )
)


def test_sample_carries_dossier_and_task() -> None:
    assert GYM_TASK.outcome == BinaryOutcome(value=True)
    sample = sample_for_task(GYM_TASK, series_dossier([RAMP], GYM_TASK.as_of))
    assert sample.files is not None
    assert {"/data/README.txt", "/data/ramp_monthly.csv"} <= set(sample.files)
    assert sample.metadata is not None
    assert sample.metadata["gym_task"]["task_id"] == GYM_TASK.task_id
    assert json.loads(str(sample.target))["value"] is True
    assert "Submit ONLY a JSON object" in str(sample.input)


def test_sandbox_compose_disables_network() -> None:
    config = yaml.safe_load(COMPOSE_PATH.read_text())
    assert config["services"]["default"]["network_mode"] == "none"


def test_agent_answers_in_sandbox(tmp_path: Path) -> None:
    # Scripted model: read the mounted dossier with bash, then submit p=0.8.
    # Proves end-to-end: sandbox up, files visible inside, tool loop, submission
    # scored with the gym's proper loss (outcome YES → log_loss = -ln(0.8)).
    model = get_model(
        "mockllm/model",
        custom_outputs=[
            ModelOutput.for_tool_call("mockllm/model", "bash", {"cmd": "head -3 /data/ramp_monthly.csv"}),
            ModelOutput.for_tool_call("mockllm/model", "submit", {"answer": json.dumps({"p": 0.8})}),
        ],
    )
    logs = inspect_eval(agent_eval_task([GYM_TASK], [RAMP]), model=model, log_dir=str(tmp_path), display="none")
    log = one(logs)
    assert log.status == "success", log.error
    assert log.samples is not None
    sample = one(log.samples)
    tool_texts = [message.text for message in sample.messages if message.role == "tool"]
    assert any("2020-01" in text for text in tool_texts), tool_texts
    assert sample.scores is not None
    assert sample.scores["gym_proper_loss"].value == pytest.approx(-math.log(0.8))


if __name__ == "__main__":
    pytest_bazel.main()
