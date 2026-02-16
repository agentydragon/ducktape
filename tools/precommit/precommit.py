"""Unified pre-commit tool: custom validations in a single Bazel invocation.

Combines custom validations to avoid Bazel client lock contention.
When pre-commit runs multiple Bazel hooks concurrently, they serialize on
the Bazel client lock, causing ~55s per hook even though actual work is <20s.

Validations:
- pytest-main check (ensures test files have pytest_bazel.main() entry points)
- terraform-version-centralization (checks terraform modules don't define provider versions)
- filename-conventions (enforces underscores not dashes in new .py/.md files)
- cluster validations (kustomize, helm, sealed-secrets)

Note: formatting moved to standard hooks (buildifier, ruff, shfmt)

Usage:
    bazel run //tools/precommit -- [files...]
    bazel run //tools/precommit  # validate all tracked files
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pygit2
from python.runfiles import runfiles

from bazel_util.workspace import get_build_workspace_directory
from tools.check_pytest_main import check_files_async
from tools.precommit.check_filename_conventions import check_filename_conventions
from tools.precommit.check_terraform_centralization import find_violations

_RUNFILES_OPT = runfiles.Create()
if _RUNFILES_OPT is None:
    raise RuntimeError("Could not create runfiles")
_RUNFILES: runfiles.Runfiles = _RUNFILES_OPT


def resolve_bin(rlocation: str) -> str:
    """Resolve a runfiles path to an absolute path."""
    path = _RUNFILES.Rlocation(rlocation)
    if not path or not Path(path).exists():
        raise RuntimeError(f"Could not resolve {rlocation}")
    return path


@dataclass
class ValidationResult:
    """Result of a validation check."""

    name: str
    elapsed: float
    success: bool
    output: str = ""
    skipped: bool = False


def is_test_file(p: Path) -> bool:
    return p.suffix == ".py" and "test_" in p.name


def is_cluster_k8s(p: Path) -> bool:
    return p.is_relative_to("cluster/k8s") and p.suffix in (".yaml", ".yml")


def is_cluster_terraform_cilium(p: Path) -> bool:
    return p.is_relative_to("cluster/terraform") and "cilium" in p.parts


def is_sealed_secret(p: Path) -> bool:
    return (p.is_relative_to("cluster/k8s") and "sealed" in p.parts) or p.is_relative_to(
        "cluster/terraform/00-persistent-auth"
    )


def is_terraform_module(p: Path) -> bool:
    return p.suffix == ".tf" and p.is_relative_to("cluster/terraform/modules")


async def run_pytest_main_check(files: list[Path], repo_root: Path) -> ValidationResult:
    """Check that test files have pytest_bazel.main() calls."""
    name = "pytest-main-check"
    test_files = [f for f in files if is_test_file(f)]
    if not test_files:
        return ValidationResult(name, 0.0, True, skipped=True)

    start = time.perf_counter()
    results = await check_files_async(test_files, repo_root)
    elapsed = time.perf_counter() - start

    failed = [r for r in results if not r.passed]
    output = "\n".join(f"{r.file_path}: {r.reason}" for r in failed) if failed else ""
    return ValidationResult(name, elapsed, not failed, output)


async def run_subprocess_validation(
    name: str,
    bin_rlocation: str,
    files: list[Path],
    file_filter: Callable[[Path], bool],
    extra_args: list[str] | None = None,
) -> ValidationResult:
    """Run a subprocess validation if any files match the filter."""
    if not any(file_filter(f) for f in files):
        return ValidationResult(name, 0.0, True, skipped=True)

    start = time.perf_counter()
    validate_bin = resolve_bin(bin_rlocation)
    cmd = [validate_bin] + (extra_args or [])
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    elapsed = time.perf_counter() - start

    output = (stdout + stderr).decode()
    return ValidationResult(name, elapsed, proc.returncode == 0, output)


async def run_terraform_centralization_check(files: list[Path]) -> ValidationResult:
    """Check terraform modules don't define provider versions."""
    name = "tf-centralization"
    if not any(is_terraform_module(f) for f in files):
        return ValidationResult(name, 0.0, True, skipped=True)

    start = time.perf_counter()
    violations = find_violations()
    elapsed = time.perf_counter() - start

    output = "\n".join(str(v) for v in violations) if violations else ""
    return ValidationResult(name, elapsed, not violations, output)


# NOTE: checkov and tofu validate were moved to standalone pre-commit hooks
# (bridgecrewio/checkov, antonbabenko/pre-commit-terraform terraform_validate)


async def run_filename_convention_check(repo: pygit2.Repository) -> ValidationResult:
    """Check that new .py/.md files and directories use underscores, not dashes."""
    name = "filename-conventions"
    start = time.perf_counter()
    violations = check_filename_conventions(repo)
    elapsed = time.perf_counter() - start
    output = "\n".join(violations) if violations else ""
    return ValidationResult(name, elapsed, not violations, output)


async def run_validate(files: list[Path], repo_root: Path, repo: pygit2.Repository) -> list[ValidationResult]:
    """Run all validations on files."""
    return list(
        await asyncio.gather(
            run_pytest_main_check(files, repo_root),
            run_terraform_centralization_check(files),
            run_filename_convention_check(repo),
            # Unified cluster validation: kustomize build, CRD layering, dependencies
            # Skip flux build in pre-commit (flaky when run in parallel) - CI runs it in isolation
            run_subprocess_validation(
                "cluster-validate",
                "_main/cluster/scripts/validate_kustomizations",
                files,
                is_cluster_k8s,
                extra_args=["--skip-flux-build"],
            ),
            run_subprocess_validation(
                "helm-validate", "_main/cluster/scripts/validate_helm_templates", files, is_cluster_terraform_cilium
            ),
            run_subprocess_validation(
                "sealed-secrets", "_main/cluster/scripts/validate_sealed_secrets", files, is_sealed_secret
            ),
        )
    )


def get_all_files(repo: pygit2.Repository) -> list[Path]:
    """Get all tracked files from git index, excluding deleted files."""
    deleted = {path for path, flags in repo.status().items() if flags & pygit2.GIT_STATUS_WT_DELETED}
    return [Path(entry.path) for entry in repo.index if entry.path not in deleted]


async def main_async() -> int:
    profile = os.environ.get("PRECOMMIT_PROFILE", "").lower() in ("1", "true", "yes")

    t0 = time.perf_counter()

    # Workspace dir needed for: pygit2.Repository("."), relative file paths, cluster script execution
    os.chdir(get_build_workspace_directory())
    repo_root = Path.cwd()
    repo = pygit2.Repository(".")
    t1 = time.perf_counter()

    # Get files to process
    files = [Path(f) for f in sys.argv[1:]] if len(sys.argv) > 1 else get_all_files(repo)
    t2 = time.perf_counter()

    if profile:
        print(f"[profile] setup: {t1 - t0:.2f}s, get_files: {t2 - t1:.2f}s")

    start_total = time.perf_counter()

    # Run validations
    print(f"Validating {len(files)} files...")
    validate_results = await run_validate(files, repo_root, repo)

    validate_failed = []
    for vresult in validate_results:
        if vresult.skipped:
            continue
        status = "✓" if vresult.success else "✗"
        print(f"{status} {vresult.name}: {vresult.elapsed:.1f}s")
        if not vresult.success:
            validate_failed.append(vresult)
            if vresult.output:
                print(vresult.output, file=sys.stderr)

    elapsed_total = time.perf_counter() - start_total
    print(f"\nTotal: {elapsed_total:.1f}s")

    if validate_failed:
        return 1

    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
