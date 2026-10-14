"""Run a frozen coding-task verifier against an agent's copied workspace."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import unittest
from pathlib import Path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--result-json", type=Path, required=True)
    args = parser.parse_args()
    workspace = args.workspace.resolve(strict=True)
    test_file = workspace / "tests" / "test_task.py"
    implementation = workspace / "jsonl_transform.py"
    if not test_file.is_file() or not implementation.is_file():
        parser.error("workspace must contain jsonl_transform.py and tests/test_task.py")

    sys.path.insert(0, str(workspace))
    suite = unittest.defaultTestLoader.discover(
        str(test_file.parent), pattern=test_file.name, top_level_dir=str(workspace)
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    payload = {
        "verifier": "frozen PR #7910 unittest tests/test_task.py",
        "workspace": str(workspace),
        "test_sha256": _sha256(test_file),
        "implementation_sha256": _sha256(implementation),
        "tests_run": result.testsRun,
        "failures": [{"test": str(test), "traceback": traceback} for test, traceback in result.failures],
        "errors": [{"test": str(test), "traceback": traceback} for test, traceback in result.errors],
        "successful": result.wasSuccessful(),
    }
    args.result_json.parent.mkdir(parents=True, exist_ok=True)
    args.result_json.write_text(json.dumps(payload, indent=2) + "\n")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
