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
    bazel run //devinfra/precommit -- [files...]
    bazel run //devinfra/precommit  # validate all tracked files
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pygit2

from cluster.scripts.validate_cluster.main import validate as validate_cluster
from cluster.validation.sealed_secrets import validate_all as validate_sealed_secrets
from devinfra.check_pytest_main import BazelPyTestIndex, build_bazel_index, check_files_async
from devinfra.precommit.check_filename_conventions import check_filename_conventions
from devinfra.precommit.check_terraform_centralization import find_violations
from util.bazel.workspace import get_build_workspace_directory

_LINT_IGNORED_ATTRS = ("linguist-generated", "gitlab-generated", "rules-lint-ignored")


def is_lint_ignored(repo: pygit2.Repository, path: Path) -> bool:
    return any(repo.get_attr(str(path), attr) in (True, "true") for attr in _LINT_IGNORED_ATTRS)


@dataclass
class Skipped:
    pass


@dataclass
class Failed:
    elapsed: float
    output: str


@dataclass
class Passed:
    elapsed: float


ValidationOutcome = Skipped | Failed | Passed


@dataclass
class ValidationResult:
    name: str
    outcome: ValidationOutcome


def is_cluster_validated(p: Path) -> bool:
    if p.is_relative_to("cluster/k8s") and p.suffix in (".yaml", ".yml"):
        return True
    return p.is_relative_to("cluster/terraform") and "cilium" in p.parts


def is_sealed_secret(p: Path) -> bool:
    return p.is_relative_to("cluster/k8s") and "sealed" in p.parts


def is_terraform_module(p: Path) -> bool:
    return p.suffix == ".tf" and p.is_relative_to("cluster/terraform/modules")


async def run_pytest_main_check(
    files: list[Path], repo_root: Path, repo: pygit2.Repository, bazel_index: BazelPyTestIndex
) -> ValidationResult:
    """Check that test files have pytest_bazel.main() calls."""
    name = "pytest-main-check"
    start = time.perf_counter()

    if not files:
        # --all mode: check every registered py_test src.
        candidates = [p.relative_to(repo_root) for p in bazel_index.known_srcs]
    else:
        # per-file mode: intersect passed files with known py_test srcs.
        candidates = [f for f in files if (repo_root / f).resolve() in bazel_index.known_srcs]

    # conftest.py files are fixture configuration — pytest doesn't run them as
    # test modules so they don't need pytest_bazel.main().
    test_files = [f for f in candidates if f.name != "conftest.py" and not is_lint_ignored(repo, f)]

    if not test_files:
        return ValidationResult(name, Skipped())

    results = await check_files_async(test_files, repo_root, bazel_index)
    elapsed = time.perf_counter() - start

    failed = [r for r in results if not r.passed]
    if failed:
        return ValidationResult(name, Failed(elapsed, "\n".join(f"{r.file_path}: {r.reason}" for r in failed)))
    return ValidationResult(name, Passed(elapsed))


async def run_cluster_validate(files: list[Path], repo_root: Path) -> ValidationResult:
    """Run cluster kustomization/helm/dependency validation."""
    name = "cluster-validate"
    if not any(is_cluster_validated(f) for f in files):
        return ValidationResult(name, Skipped())

    start = time.perf_counter()
    kust_errors, global_errors = await validate_cluster(repo_root / "cluster/k8s", skip_flux_build=True)
    elapsed = time.perf_counter() - start

    if kust_errors or global_errors:
        lines = [f"  {k.parent}: {err.strip()}" for k, err in kust_errors]
        lines.extend(f"  {err.strip()}" for err in global_errors)
        return ValidationResult(name, Failed(elapsed, "\n".join(lines)))
    return ValidationResult(name, Passed(elapsed))


async def run_sealed_secrets_validate(files: list[Path]) -> ValidationResult:
    """Validate SealedSecrets can be decrypted with tofu keypair."""
    name = "sealed-secrets"
    if not any(is_sealed_secret(f) for f in files):
        return ValidationResult(name, Skipped())

    start = time.perf_counter()
    errors = await validate_sealed_secrets()
    elapsed = time.perf_counter() - start

    if errors:
        return ValidationResult(name, Failed(elapsed, "\n".join(errors)))
    return ValidationResult(name, Passed(elapsed))


async def run_terraform_centralization_check(files: list[Path], repo_root: Path) -> ValidationResult:
    """Check terraform modules don't define provider versions."""
    name = "tf-centralization"
    if not any(is_terraform_module(f) for f in files):
        return ValidationResult(name, Skipped())

    start = time.perf_counter()
    violations = find_violations(repo_root)
    elapsed = time.perf_counter() - start

    if violations:
        return ValidationResult(name, Failed(elapsed, "\n".join(str(v) for v in violations)))
    return ValidationResult(name, Passed(elapsed))


async def run_filename_convention_check(repo: pygit2.Repository) -> ValidationResult:
    """Check that new .py/.md files and directories use underscores, not dashes."""
    name = "filename-conventions"
    start = time.perf_counter()
    violations = check_filename_conventions(repo)
    elapsed = time.perf_counter() - start
    if violations:
        return ValidationResult(name, Failed(elapsed, "\n".join(violations)))
    return ValidationResult(name, Passed(elapsed))


async def run_validate(
    files: list[Path], repo_root: Path, repo: pygit2.Repository, bazel_index: BazelPyTestIndex
) -> list[ValidationResult]:
    """Run all validations on files."""
    return list(
        await asyncio.gather(
            run_pytest_main_check(files, repo_root, repo, bazel_index),
            run_terraform_centralization_check(files, repo_root),
            run_filename_convention_check(repo),
            run_cluster_validate(files, repo_root),
            run_sealed_secrets_validate(files),
        )
    )


def get_all_files(repo: pygit2.Repository) -> list[Path]:
    """Get all tracked files from git index, excluding deleted files.

    Uses per-entry exists() instead of repo.status() — the latter triggers
    ~160k syscalls (stat/readlink/access per file) and takes ~88s on 9p filesystems.
    """
    repo_root = Path(repo.workdir)
    return [Path(entry.path) for entry in repo.index if (repo_root / entry.path).exists()]


async def main_async() -> int:
    profile = os.environ.get("PRECOMMIT_PROFILE", "").lower() in ("1", "true", "yes")

    t0 = time.perf_counter()

    repo_root = get_build_workspace_directory()
    repo = pygit2.Repository(str(repo_root))
    t1 = time.perf_counter()

    all_mode = len(sys.argv) > 1 and sys.argv[1] == "--all"

    bazel_index = build_bazel_index(repo_root)

    # Get files to process: --all skips the file list (index drives the check),
    # otherwise use argv files or fall back to all tracked files.
    if all_mode:
        files: list[Path] = []
    elif len(sys.argv) > 1:
        files = [Path(f) for f in sys.argv[1:]]
    else:
        files = get_all_files(repo)
    t2 = time.perf_counter()

    if profile:
        print(f"[profile] setup: {t1 - t0:.2f}s, get_files: {t2 - t1:.2f}s")

    start_total = time.perf_counter()

    # Run validations
    if all_mode:
        print("Checking all py_test srcs in repository...")
    else:
        print(f"Validating {len(files)} files...")
    validate_results = await run_validate(files, repo_root, repo, bazel_index)

    validate_failed = []
    for vresult in validate_results:
        match vresult.outcome:
            case Skipped():
                pass
            case Passed(elapsed=elapsed):
                print(f"✓ {vresult.name}: {elapsed:.1f}s")
            case Failed(elapsed=elapsed, output=output):
                print(f"✗ {vresult.name}: {elapsed:.1f}s")
                validate_failed.append(vresult)
                if output:
                    print(output, file=sys.stderr)

    elapsed_total = time.perf_counter() - start_total
    print(f"\nTotal: {elapsed_total:.1f}s")

    if validate_failed:
        return 1

    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
