"""One analyzed bundle accepts two different module layouts and preserves behavior."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import pytest_bazel
from python.runfiles import Runfiles


@pytest.mark.parametrize(("output", "modules"), [("OUTPUT_A", {"values.js"}), ("OUTPUT_B", {"input.js", "double.js"})])
def test_shared_analysis_accepts_both_layouts(output: str, modules: set[str]) -> None:
    runfiles = Runfiles.Create()
    assert runfiles is not None
    root = runfiles.Rlocation(os.environ[output])
    node = runfiles.Rlocation(os.environ["NODE"])
    assert root is not None
    assert node is not None
    tree = Path(root)
    chunk = tree / "app/main"
    assert {path.name for path in chunk.glob("*.js")} == modules | {"entry.js"}
    assert not (tree / "GATE_REJECTED").exists()
    validation = json.loads((tree / "reports/tree/main/translation_validation.json").read_text())
    assert validation["gate_accepts"]
    assert validation["accepts"]
    result = subprocess.run(
        [
            node,
            "--experimental-default-type=module",
            "--input-type=module",
            "-e",
            "import {pathToFileURL} from 'node:url'; "
            "const {input, double} = await import(pathToFileURL(process.argv[1])); "
            "console.log(JSON.stringify({input, double}));",
            str(chunk / "entry.js"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {"input": 2, "double": 4}


if __name__ == "__main__":
    pytest_bazel.main()
