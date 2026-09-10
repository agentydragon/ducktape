"""Persist an execution input for reproducible Python-owned experiment sessions."""

import json
from pathlib import Path

from finance.augur.sim.backend import CompiledRun


def write_prepared_input(run: CompiledRun, path: Path) -> None:
    path.write_text(json.dumps(run.execution_input))
