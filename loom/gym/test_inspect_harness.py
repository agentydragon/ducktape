from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
import pytest_bazel
import yaml

# Aliased to avoid shadowing the builtin.
from inspect_ai import eval as inspect_eval
from inspect_ai.model import ModelOutput, get_model
from more_itertools import one

from loom.gym.inspect_harness import COMPOSE_PATH, agent_eval_task, sample_for_task
from loom.gym.series_tasks import series_tasks

# Known-history task: anchor 2024-06 close 5460.48, threshold 5733.50, crossed → YES.
GYM_TASK = one(task for task in series_tasks() if task.task_id == "sp500-ge-1.05x-2024-06-h6")


def test_sample_carries_dossier_and_task() -> None:
    sample = sample_for_task(GYM_TASK)
    assert sample.files is not None
    assert {"/data/README.txt", "/data/sp500_monthly.csv"} <= set(sample.files)
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
            ModelOutput.for_tool_call("mockllm/model", "bash", {"cmd": "head -2 /data/sp500_monthly.csv"}),
            ModelOutput.for_tool_call("mockllm/model", "submit", {"answer": json.dumps({"p": 0.8})}),
        ],
    )
    logs = inspect_eval(agent_eval_task([GYM_TASK]), model=model, log_dir=str(tmp_path), display="none")
    log = one(logs)
    assert log.status == "success", log.error
    assert log.samples is not None
    sample = one(log.samples)
    tool_texts = [message.text for message in sample.messages if message.role == "tool"]
    assert any("2013-01" in text for text in tool_texts), tool_texts
    assert sample.scores is not None
    assert sample.scores["gym_proper_loss"].value == pytest.approx(-math.log(0.8))


if __name__ == "__main__":
    pytest_bazel.main()
