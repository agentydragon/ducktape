"""Kyverno CLI wrapper for policy testing."""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from util.bazel.runfiles import get_required_path


def _kyverno_bin() -> Path:
    return get_required_path("multitool/tools/kyverno/kyverno")


@dataclass(frozen=True)
class KyvernoApplyResult:
    """Parsed output of `kyverno apply`."""

    passed: int
    failed: int
    warned: int
    errored: int
    skipped: int
    stdout: str
    mutated_resources: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.errored == 0


_SUMMARY_RE = re.compile(r"pass:\s*(\d+),\s*fail:\s*(\d+),\s*warn:\s*(\d+),\s*error:\s*(\d+),\s*skip:\s*(\d+)")


def apply_policy(policy_path: Path, resource_path: Path, set_vars: dict[str, str] | None = None) -> KyvernoApplyResult:
    """Run `kyverno apply` against a resource, parse summary and mutated output.

    `set_vars` feeds `--set name=value` pairs into the run. `kyverno apply` unconditionally
    disables `context[].apiCall` loading outside `--cluster` mode (verified against this
    pinned CLI: passing the apiCall's target as an extra `--resource` has no effect, and the
    engine logs "disabled loading of APICall context entry" regardless) -- so a policy whose
    precondition depends on an apiCall-populated variable can only be exercised offline by
    supplying that variable's value directly, keyed by the same name the `context` entry
    would have bound it to.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        args: list[str | Path] = [
            _kyverno_bin(),
            "apply",
            str(policy_path),
            "--resource",
            str(resource_path),
            "-o",
            tmpdir,
        ]
        if set_vars:
            args += ["--set", ",".join(f"{name}={value}" for name, value in set_vars.items())]
        result = subprocess.run(args, check=False, capture_output=True, text=True)
        stdout = result.stdout + result.stderr
        match = _SUMMARY_RE.search(stdout)
        if not match:
            raise RuntimeError(f"Could not parse kyverno apply output:\n{stdout}")
        mutated: list[dict] = []
        for p in sorted(Path(tmpdir).glob("*.yaml")):
            mutated.extend(doc for doc in yaml.safe_load_all(p.read_text()) if doc is not None)
        return KyvernoApplyResult(
            passed=int(match.group(1)),
            failed=int(match.group(2)),
            warned=int(match.group(3)),
            errored=int(match.group(4)),
            skipped=int(match.group(5)),
            stdout=stdout,
            mutated_resources=mutated,
        )
