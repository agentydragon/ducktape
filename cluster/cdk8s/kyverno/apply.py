"""Kyverno CLI wrapper for policy testing."""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from python.runfiles import Runfiles


def _kyverno_bin() -> Path:
    return Runfiles.CreateOrRaise().root() / "multitool/tools/kyverno/kyverno"


@dataclass(frozen=True)
class KyvernoApplyResult:
    """Parsed output of `kyverno apply`."""

    passed: int
    failed: int
    errored: int
    stdout: str
    mutated_resources: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.errored == 0


_SUMMARY_RE = re.compile(r"pass:\s*(\d+),\s*fail:\s*(\d+),\s*warn:\s*\d+,\s*error:\s*(\d+),\s*skip:\s*\d+")


def apply_policy(
    policy_path: Path, resource_path: Path, *, json_payload: bool = False, userinfo_path: Path | None = None
) -> KyvernoApplyResult:
    """Run `kyverno apply` against a resource, parse summary and mutated output.

    Offline `kyverno apply` never loads `context[].apiCall` entries, so a policy that needs one is not testable here.

    `json_payload` passes the file as `--json` instead of `--resource`: for a `policies.kyverno.io`
    policy in `evaluation.mode: JSON`, the only way to
    evaluate a custom-resource kind, because offline `kyverno apply` cannot map a CRD's kind to a
    resource ("failed to map gvk to gvr") and has no flag to load the CRD.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        args: list[str | Path] = [
            _kyverno_bin(),
            "apply",
            str(policy_path),
            "--json" if json_payload else "--resource",
            str(resource_path),
            "-o",
            tmpdir,
        ]
        if userinfo_path:
            args += ["--userinfo", str(userinfo_path)]
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
            errored=int(match.group(3)),
            stdout=stdout,
            mutated_resources=mutated,
        )
