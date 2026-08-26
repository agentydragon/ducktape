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


def apply_twice(
    policy_path: Path, resource_path: Path, tmp_path: Path, set_vars: dict[str, str] | None = None, kind: str = "Pod"
) -> tuple[KyvernoApplyResult, KyvernoApplyResult]:
    """Simulate Kyverno reinvoking a mutating policy within the same admission request.

    Kyverno's mutating webhook is registered `reinvocationPolicy: IfNeeded`, so when a
    later-ordered webhook mutates the resource first, Kyverno runs again on the same
    CREATE and sees its own output as input. Feeds the first pass's output back through
    the same policy and returns both results; what "idempotent" means for the specific
    mutation (no duplicate list entries for an RFC 6902 append, an unchanged field for a
    map merge, ...) is for the caller to assert.
    """
    first = apply_policy(policy_path, resource_path, set_vars)
    assert first.ok, first.stdout
    reapplied = tmp_path / "reinvoked.yaml"
    reapplied.write_text(yaml.safe_dump(next(d for d in first.mutated_resources if d["kind"] == kind)))
    second = apply_policy(policy_path, reapplied, set_vars)
    assert second.ok, second.stdout
    return first, second


def assert_not_mutated(policy_path: Path, resource_path: Path, set_vars: dict[str, str] | None = None) -> None:
    """A rule that doesn't match must skip cleanly and leave the resource untouched --
    not just missing the one field a caller happens to check for, which would miss an
    unrelated accidental mutation.
    """
    original = yaml.safe_load(resource_path.read_text())
    result = apply_policy(policy_path, resource_path, set_vars)
    assert result.ok, result.stdout
    assert result.skipped >= 1, f"expected the rule to skip\n{result.stdout}"
    assert result.mutated_resources == [original], result.stdout
