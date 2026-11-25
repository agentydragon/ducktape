"""A moved Error stack observation is outside the certified fragment."""

import json
import os
import subprocess
from pathlib import Path

import pytest_bazel
from python.runfiles import Runfiles

ENTRY = "main.mjs"
OWNER = "parts/register.mjs"


def test_relocated_stack_key_is_refused(tmp_path: Path) -> None:
    source = tmp_path / ENTRY
    source.write_text("globalThis.registry[new Error().stack] = 1;\n")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"entry": ENTRY, "modules": {ENTRY: [], OWNER: ["#0"]}, "order": [OWNER]}))
    out = tmp_path / "split"
    request = tmp_path / "split-request.json"
    request.write_text(
        json.dumps({"source": str(source), "spec": str(spec), "out": str(out), "require": "normal-completion"})
    )

    runfiles = Runfiles.CreateOrRaise()
    split_helper = runfiles.Rlocation(os.environ["SPLIT_HELPER"])
    assert split_helper is not None
    checked = subprocess.run([split_helper, str(request)], text=True, capture_output=True, check=False)
    assert checked.returncode == 0, checked.stderr
    verdict = json.loads(checked.stdout)
    assert verdict["status"] == "refused", checked.stdout
    assert verdict["reason"], checked.stdout
    assert verdict["reasonCode"] in {"probe_unknown", "linked_input"}, checked.stdout
    assert not out.exists(), "a refused stack observation must not emit modules"


if __name__ == "__main__":
    pytest_bazel.main()
