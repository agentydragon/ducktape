"""Unified pre-commit tool: format + validate in a single Bazel invocation.

Combines formatting and validation to avoid Bazel client lock contention.
When pre-commit runs multiple Bazel hooks concurrently, they serialize on
the Bazel client lock, causing ~55s per hook even though actual work is <20s.

This single binary runs both in sequence within one Bazel invocation:
1. Format: prettier, ruff, shfmt, buildifier
2. Validate: buildifier-lint, pytest-main check, cluster validations

Usage:
    bazel run //tools/precommit -- [files...]
    bazel run //tools/precommit  # format and validate all tracked files
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pygit2
from checkov.runner_filter import RunnerFilter
from checkov.terraform.runner import Runner as CheckovTerraformRunner
from python.runfiles import runfiles

from tools.check_pytest_main import check_files_async
from tools.env_utils import get_workspace_dir
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


EXTENSION_MAP: dict[str, str] = {
    ".js": "prettier",
    ".jsx": "prettier",
    ".ts": "prettier",
    ".tsx": "prettier",
    ".css": "prettier",
    ".html": "prettier",
    ".md": "prettier",
    ".json": "prettier",
    ".yaml": "prettier",
    ".yml": "prettier",
    ".svelte": "prettier",
    ".py": "ruff",
    ".sh": "shfmt",
    ".bash": "shfmt",
    ".bzl": "buildifier",
    ".bazel": "buildifier",
}

FILENAME_MAP: dict[str, str] = {
    "BUILD": "buildifier",
    "BUILD.bazel": "buildifier",
    "WORKSPACE": "buildifier",
    "WORKSPACE.bazel": "buildifier",
}

SHELL_SHEBANG_RE = re.compile(rb"^#![ \t]*/(usr/)?bin/(env[ \t]+)?(sh|bash|mksh|bats|zsh)")
IGNORE_ATTRIBUTES = ("linguist-generated", "gitlab-generated", "rules-lint-ignored")


def get_max_batch_size() -> int:
    """Get max command-line size, matching rules_lint behavior."""
    try:
        arg_max = os.sysconf("SC_ARG_MAX")
    except (ValueError, OSError):
        arg_max = 128000
    return min(arg_max - 2048, 128000)


def batch_files(files: list[str], max_size: int) -> list[list[str]]:
    """Split files into batches that fit within ARG_MAX."""
    batches: list[list[str]] = []
    batch: list[str] = []
    for f in files:
        if batch and len(" ".join(batch)) + 1 + len(f) >= max_size:
            batches.append(batch)
            batch = []
        batch.append(f)
    return [*batches, batch] if batch else batches


def detect_shell_by_shebang(path: Path) -> bool:
    """Check if file has a shell shebang (for files without .sh extension)."""
    if path.suffix:
        return False
    try:
        with path.open("rb") as f:
            first_line = f.readline(256)
        return bool(SHELL_SHEBANG_RE.match(first_line))
    except OSError:
        return False


def get_formatter(path: Path) -> str | None:
    """Determine which formatter to use for a file."""
    if path.name in FILENAME_MAP:
        return FILENAME_MAP[path.name]
    if formatter := EXTENSION_MAP.get(path.suffix.lower()):
        return formatter
    if detect_shell_by_shebang(path):
        return "shfmt"
    return None


def filter_ignored(repo: pygit2.Repository, files: list[Path]) -> list[Path]:
    """Filter out files marked as ignored via .gitattributes."""
    if not files:
        return []
    return [f for f in files if not any(repo.get_attr(str(f), attr) in (True, "true") for attr in IGNORE_ATTRIBUTES)]


@dataclass
class FormatterResult:
    """Result of running a formatter."""

    formatter: str
    file_count: int
    elapsed: float
    success: bool
    errors: list[str] = field(default_factory=list)


def resolve_formatter_bin(formatter: str) -> str:
    """Resolve formatter binary path from environment. Raises if not found."""
    bin_var = f"{formatter.upper()}_BIN"
    if not (rlocation_path := os.environ.get(bin_var)):
        raise RuntimeError(f"{bin_var} environment variable not set")
    if not (bin_path := _RUNFILES.Rlocation(rlocation_path)) or not Path(bin_path).exists():
        raise RuntimeError(f"Could not resolve {rlocation_path}")
    return bin_path


FORMATTER_COMMANDS: dict[str, Callable[[str, bool], list[str]]] = {
    "prettier": lambda bin_path, check: [bin_path, "--check" if check else "--write"],
    "ruff": lambda bin_path, check: [bin_path, "format", *(["--check"] if check else [])],
    "shfmt": lambda bin_path, check: [bin_path, "-d" if check else "-w"],
    "buildifier": lambda bin_path, _: [bin_path],
}


async def run_format_batch(base_cmd: list[str], batch: list[str]) -> tuple[int, str]:
    """Run formatter on a batch of files. Returns (returncode, combined output)."""
    proc = await asyncio.create_subprocess_exec(
        *base_cmd, *batch, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    output = (stdout.decode() + stderr.decode()).strip()
    return proc.returncode or 0, output


async def run_formatter_async(formatter: str, files: list[Path], check_mode: bool) -> FormatterResult:
    """Run a formatter on files asynchronously, parallelizing batches."""
    if not files:
        return FormatterResult(formatter=formatter, file_count=0, elapsed=0.0, success=True)

    file_paths = [str(f) for f in files]
    bin_path = resolve_formatter_bin(formatter)
    base_cmd = FORMATTER_COMMANDS[formatter](bin_path, check_mode)

    batches = batch_files(file_paths, get_max_batch_size())
    start = time.perf_counter()

    results = await asyncio.gather(*[run_format_batch(base_cmd, batch) for batch in batches])

    errors = [output for returncode, output in results if returncode != 0 and output]
    elapsed = time.perf_counter() - start
    return FormatterResult(
        formatter=formatter, file_count=len(file_paths), elapsed=elapsed, success=not errors, errors=errors
    )


async def run_format(repo: pygit2.Repository, files: list[Path], check_mode: bool) -> list[FormatterResult]:
    """Run all formatters on files."""
    files = filter_ignored(repo, files)

    by_formatter: dict[str, list[Path]] = defaultdict(list)
    for f in files:
        if formatter := get_formatter(f):
            by_formatter[formatter].append(f)

    return list(
        await asyncio.gather(
            *[run_formatter_async(fmt, fmt_files, check_mode) for fmt, fmt_files in by_formatter.items()]
        )
    )


@dataclass
class ValidationResult:
    """Result of a validation check."""

    name: str
    elapsed: float
    success: bool
    output: str = ""
    skipped: bool = False


def is_bazel_file(p: Path) -> bool:
    return p.name in ("BUILD", "BUILD.bazel", "WORKSPACE", "WORKSPACE.bazel") or p.suffix in (".bzl", ".bazel")


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


def is_terraform_file(p: Path) -> bool:
    return p.suffix == ".tf" and p.is_relative_to("cluster/terraform")


async def run_buildifier_lint(files: list[Path]) -> ValidationResult:
    """Run buildifier lint on Bazel files."""
    name = "buildifier-lint"
    bazel_files = [str(f) for f in files if is_bazel_file(f)]
    if not bazel_files:
        return ValidationResult(name, 0.0, True, skipped=True)

    start = time.perf_counter()
    buildifier = resolve_bin("buildifier_prebuilt/buildifier/buildifier")
    proc = await asyncio.create_subprocess_exec(
        buildifier,
        "--mode=check",
        "--lint=warn",
        *bazel_files,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    elapsed = time.perf_counter() - start

    output = (stdout + stderr).decode()
    return ValidationResult(name, elapsed, proc.returncode == 0, output)


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
    name: str, bin_rlocation: str, files: list[Path], file_filter: Callable[[Path], bool]
) -> ValidationResult:
    """Run a subprocess validation if any files match the filter."""
    if not any(file_filter(f) for f in files):
        return ValidationResult(name, 0.0, True, skipped=True)

    start = time.perf_counter()
    validate_bin = resolve_bin(bin_rlocation)
    proc = await asyncio.create_subprocess_exec(
        validate_bin, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
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


async def run_tflint(files: list[Path], repo_root: Path) -> ValidationResult:
    """Run tflint on terraform files."""
    name = "tflint"
    tf_files = [f for f in files if is_terraform_file(f)]
    if not tf_files:
        return ValidationResult(name, 0.0, True, skipped=True)

    start = time.perf_counter()
    tflint_bin = resolve_formatter_bin("tflint")
    tf_dirs = {f.parent for f in tf_files}
    config_path = repo_root / "cluster" / ".tflint.hcl"

    # Initialize plugins (downloads terraform ruleset if not cached)
    init_proc = await asyncio.create_subprocess_exec(
        tflint_bin, "--init", f"--config={config_path}", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    await init_proc.communicate()

    # Run tflint on each directory
    tasks = []
    for tf_dir in tf_dirs:
        tasks.append(
            asyncio.create_subprocess_exec(
                tflint_bin,
                f"--chdir={tf_dir}",
                f"--config={config_path}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        )

    procs = await asyncio.gather(*tasks)
    results = await asyncio.gather(*[p.communicate() for p in procs])
    elapsed = time.perf_counter() - start

    failed_outputs = []
    for proc, (stdout, stderr) in zip(procs, results, strict=True):
        if proc.returncode != 0:
            output = (stdout + stderr).decode().strip()
            if output:
                failed_outputs.append(output)

    return ValidationResult(name, elapsed, not failed_outputs, "\n".join(failed_outputs))


async def run_tofu_validate(files: list[Path]) -> list[ValidationResult]:
    """Run tofu validate on terraform directories."""
    tf_files = [f for f in files if is_terraform_file(f)]
    if not tf_files:
        return []

    tofu_bin = resolve_formatter_bin("tofu")
    tf_dirs = {f.parent for f in tf_files}
    results = []

    for tf_dir in tf_dirs:
        name = f"tofu-validate:{tf_dir.name}"
        start = time.perf_counter()

        # Init first (required for validate)
        init_proc = await asyncio.create_subprocess_exec(
            tofu_bin,
            f"-chdir={tf_dir}",
            "init",
            "-backend=false",
            "-input=false",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await init_proc.communicate()

        # Validate
        proc = await asyncio.create_subprocess_exec(
            tofu_bin, f"-chdir={tf_dir}", "validate", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        elapsed = time.perf_counter() - start

        output = (stdout + stderr).decode().strip()
        results.append(ValidationResult(name, elapsed, proc.returncode == 0, output if proc.returncode != 0 else ""))

    return results


async def run_checkov(files: list[Path]) -> ValidationResult:
    """Run checkov security scanner on terraform files."""
    name = "checkov"
    tf_files = [f for f in files if is_terraform_file(f)]
    if not tf_files:
        return ValidationResult(name, 0.0, True, skipped=True)

    start = time.perf_counter()

    runner = CheckovTerraformRunner()
    runner_filter = RunnerFilter(skip_checks=["CKV_TF_1"])

    # Run in executor to not block the event loop
    loop = asyncio.get_event_loop()
    report = await loop.run_in_executor(
        None, lambda: runner.run(root_folder="cluster/terraform", runner_filter=runner_filter)
    )
    elapsed = time.perf_counter() - start

    # Check for failures — checkov runner.run() can return Report or list[Report]
    if isinstance(report, list):
        failed_checks = [c for r in report for c in r.failed_checks]
    elif report:
        failed_checks = report.failed_checks
    else:
        failed_checks = []
    if failed_checks:
        output_lines = [f"{c.resource}: {c.check_id}" for c in failed_checks]
        return ValidationResult(name, elapsed, False, "\n".join(output_lines))

    return ValidationResult(name, elapsed, True, "")


async def run_validate(files: list[Path], repo_root: Path) -> list[ValidationResult]:
    """Run all validations on files."""
    # Run tofu validate separately since it returns multiple results
    tofu_results = await run_tofu_validate(files)

    other_results = list(
        await asyncio.gather(
            run_buildifier_lint(files),
            run_pytest_main_check(files, repo_root),
            run_terraform_centralization_check(files),
            run_tflint(files, repo_root),
            run_checkov(files),
            run_subprocess_validation(
                "kustomize-validate", "_main/cluster/scripts/validate_kustomizations", files, is_cluster_k8s
            ),
            run_subprocess_validation(
                "flux-validate", "_main/cluster/scripts/validate_flux_build", files, is_cluster_k8s
            ),
            run_subprocess_validation(
                "gitops-deps", "_main/cluster/scripts/validate_dependencies", files, is_cluster_k8s
            ),
            run_subprocess_validation(
                "helm-validate", "_main/cluster/scripts/validate_helm_templates", files, is_cluster_terraform_cilium
            ),
            run_subprocess_validation(
                "sealed-secrets", "_main/cluster/scripts/validate_sealed_secrets", files, is_sealed_secret
            ),
        )
    )

    return other_results + tofu_results


def get_all_files(repo: pygit2.Repository) -> list[Path]:
    """Get all tracked files from git index (fast, unlike pygit2.status())."""
    return [Path(entry.path) for entry in repo.index]


async def main_async() -> int:
    profile = os.environ.get("PRECOMMIT_PROFILE", "").lower() in ("1", "true", "yes")
    check_mode = os.environ.get("FMT_CHECK", "").lower() in ("1", "true", "yes")

    t0 = time.perf_counter()

    # Workspace dir needed for: pygit2.Repository("."), relative file paths, cluster script execution
    os.chdir(get_workspace_dir())
    repo_root = Path.cwd()
    repo = pygit2.Repository(".")
    t1 = time.perf_counter()

    # Get files to process
    files = [Path(f) for f in sys.argv[1:]] if len(sys.argv) > 1 else get_all_files(repo)
    t2 = time.perf_counter()

    if profile:
        print(f"[profile] setup: {t1 - t0:.2f}s, get_files: {t2 - t1:.2f}s")

    start_total = time.perf_counter()

    # Run format
    print(f"Formatting {len(files)} files...")
    format_results = await run_format(repo, files, check_mode)

    format_failed = []
    for result in format_results:
        if result.file_count > 0:
            status = "✓" if result.success else "✗"
            print(f"{status} {result.formatter}: {result.file_count} files in {result.elapsed:.1f}s")
        if result.errors:
            format_failed.append(result)
            for error in result.errors:
                print(error, file=sys.stderr)

    # Run validate
    print(f"\nValidating {len(files)} files...")
    validate_results = await run_validate(files, repo_root)

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

    if format_failed or validate_failed:
        if check_mode and format_failed:
            print("Try running 'bazel run //tools/precommit' to fix formatting.", file=sys.stderr)
        return 1

    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
