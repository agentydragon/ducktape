"""File transport for experiment-owned native policy runners.

The caller owns the compiled paths, runner, parameters and output analysis. Rust
deserializes the prepared input and its engine entrypoint validates it; this module
does not reinterpret the execution document or select a policy/capture mode.
"""

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from finance.augur.sim.backend import CompiledRun


def write_prepared_input(run: CompiledRun, path: Path) -> None:
    path.write_text(json.dumps(run.execution_input))


def invoke(*, binary: Path, input_path: Path, output_path: Path, arguments: Sequence[str]) -> dict[str, Any]:
    """Run INPUT OUTPUT ARGS; decode the existing output only after a successful exit.

    Parameters are argv entries, never shell text. Native diagnostics pass through;
    process, file and JSON errors propagate to the experiment's caller.
    """
    subprocess.run([binary, input_path, output_path, *arguments], check=True)
    document = json.loads(output_path.read_text())
    if not isinstance(document, dict):
        raise ValueError("native runner output must be a JSON object")
    return document
