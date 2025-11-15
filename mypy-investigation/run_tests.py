#!/usr/bin/env python3
"""
Comprehensive type checker testing framework.

Tests the same Python files with various type checkers and configurations
to isolate the Final[str] cross-module type inference issue.
"""
import json
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass
class TestResult:
    """Result of a single type checker run."""
    checker: str
    version: str
    config: dict[str, Any]
    passed: bool
    errors: list[str]
    output: str

    def to_dict(self) -> dict:
        return asdict(self)


class TypeCheckerAdapter(ABC):
    """Base adapter for running type checkers."""

    def __init__(self, test_dir: Path):
        self.test_dir = test_dir

    @abstractmethod
    def get_name(self) -> str:
        """Return the name of this type checker."""
        pass

    @abstractmethod
    def get_version(self) -> str:
        """Return the version being tested."""
        pass

    @abstractmethod
    def run(self, config: dict[str, Any]) -> TestResult:
        """Run the type checker with given configuration."""
        pass

    def _count_errors(self, output: str, pattern: str) -> list[str]:
        """Extract error lines matching pattern."""
        return [line for line in output.split('\n') if pattern in line]


class MypyAdapter(TypeCheckerAdapter):
    """Adapter for mypy type checker."""

    def __init__(self, test_dir: Path, mypy_version: str, venv_dir: Path):
        super().__init__(test_dir)
        self.mypy_version = mypy_version
        self.venv_dir = venv_dir
        self.mypy_bin = venv_dir / "bin" / "mypy"

    def get_name(self) -> str:
        return "mypy"

    def get_version(self) -> str:
        return self.mypy_version

    def _create_config(self, config: dict[str, Any]) -> Path:
        """Create mypy.ini with given config."""
        config_file = self.test_dir / "mypy.ini"
        config_lines = ["[mypy]"]
        for key, value in config.items():
            if isinstance(value, bool):
                config_lines.append(f"{key} = {'true' if value else 'false'}")
            else:
                config_lines.append(f"{key} = {value}")
        config_file.write_text("\n".join(config_lines))
        return config_file

    def run(self, config: dict[str, Any]) -> TestResult:
        """Run mypy with configuration."""
        config_file = self._create_config(config)

        try:
            result = subprocess.run(
                [str(self.mypy_bin), "--config-file", str(config_file), str(self.test_dir)],
                capture_output=True,
                text=True,
                timeout=30
            )
            output = result.stdout + result.stderr
            errors = self._count_errors(output, "no-any-return")
            passed = result.returncode == 0 and len(errors) == 0

            return TestResult(
                checker=self.get_name(),
                version=self.get_version(),
                config=config,
                passed=passed,
                errors=errors,
                output=output
            )
        finally:
            config_file.unlink(missing_ok=True)


class PyrightAdapter(TypeCheckerAdapter):
    """Adapter for pyright type checker."""

    def __init__(self, test_dir: Path):
        super().__init__(test_dir)

    def get_name(self) -> str:
        return "pyright"

    def get_version(self) -> str:
        try:
            result = subprocess.run(
                ["pyright", "--version"],
                capture_output=True,
                text=True,
                timeout=10
            )
            # Parse version from output like "pyright 1.1.407"
            version = result.stdout.strip().split()[-1]
            return version
        except Exception:
            return "unknown"

    def run(self, config: dict[str, Any]) -> TestResult:
        """Run pyright."""
        python_version = config.get("python_version", "3.12")

        try:
            result = subprocess.run(
                ["pyright", f"--pythonversion={python_version}", str(self.test_dir)],
                capture_output=True,
                text=True,
                timeout=30
            )
            output = result.stdout + result.stderr
            # Pyright doesn't have warn_return_any, so we check for general errors
            passed = result.returncode == 0 and "error" not in output.lower()

            return TestResult(
                checker=self.get_name(),
                version=self.get_version(),
                config=config,
                passed=passed,
                errors=[],
                output=output
            )
        except FileNotFoundError:
            return TestResult(
                checker=self.get_name(),
                version="not-installed",
                config=config,
                passed=False,
                errors=["Pyright not installed"],
                output="Pyright not found"
            )


class PreCommitMypyAdapter(TypeCheckerAdapter):
    """Adapter for mypy running via pre-commit."""

    def __init__(self, test_dir: Path, mypy_version: str):
        super().__init__(test_dir)
        self.mypy_version = mypy_version
        self.repo_dir = test_dir.parent

    def get_name(self) -> str:
        return "pre-commit-mypy"

    def get_version(self) -> str:
        return self.mypy_version

    def _create_precommit_config(self, config: dict[str, Any]) -> Path:
        """Create .pre-commit-config.yaml."""
        args = [f"--python-version={config.get('python_version', '3.12')}"]
        if config.get("warn_return_any"):
            args.append("--warn-return-any")
        if config.get("disable_expression_cache"):
            args.append("--disable-expression-cache")

        config_content = f"""
repos:
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v{self.mypy_version}
    hooks:
      - id: mypy
        args: {args}
        files: "test_case/.*\\\\.py$"
"""
        config_file = self.repo_dir / ".pre-commit-config.yaml"
        config_file.write_text(config_content)
        return config_file

    def run(self, config: dict[str, Any]) -> TestResult:
        """Run mypy via pre-commit."""
        config_file = self._create_precommit_config(config)

        # Initialize git if needed
        git_dir = self.repo_dir / ".git"
        if not git_dir.exists():
            subprocess.run(["git", "init"], cwd=self.repo_dir, capture_output=True)
            subprocess.run(["git", "add", "."], cwd=self.repo_dir, capture_output=True)

        try:
            result = subprocess.run(
                ["pre-commit", "run", "mypy", "--all-files"],
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
                timeout=120
            )
            output = result.stdout + result.stderr
            errors = self._count_errors(output, "no-any-return")
            passed = "Passed" in output and len(errors) == 0

            return TestResult(
                checker=self.get_name(),
                version=self.get_version(),
                config=config,
                passed=passed,
                errors=errors,
                output=output
            )
        finally:
            config_file.unlink(missing_ok=True)


def setup_mypy_venv(version: str, results_dir: Path) -> Path:
    """Create virtualenv with specific mypy version."""
    venv_dir = results_dir / f"venv-mypy-{version}"
    if not venv_dir.exists():
        print(f"  Creating venv for mypy {version}...")
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
        subprocess.run(
            [str(venv_dir / "bin" / "pip"), "install", "--quiet", f"mypy=={version}"],
            check=True
        )
    return venv_dir


def main():
    """Run comprehensive type checker tests."""
    script_dir = Path(__file__).parent
    test_dir = script_dir / "test_case"
    results_dir = script_dir / "results"
    results_dir.mkdir(exist_ok=True)

    print("=" * 70)
    print("Type Checker Investigation: Final[str] Cross-Module Type Inference")
    print("=" * 70)
    print()

    all_results: list[TestResult] = []

    # Test configurations to try
    configs = [
        {"python_version": "3.12", "warn_return_any": True},
        {"python_version": "3.12", "warn_return_any": True, "disable_expression_cache": True},
    ]

    # Test with Pyright
    print("Testing with Pyright...")
    pyright = PyrightAdapter(test_dir)
    for config in configs[:1]:  # Pyright doesn't have these config options
        result = pyright.run(config)
        all_results.append(result)
        status = "✓ PASSED" if result.passed else "✗ FAILED"
        print(f"  {status}: {pyright.get_name()} {pyright.get_version()}")
    print()

    # Test with various mypy versions
    mypy_versions = ["1.14.0", "1.15.0", "1.16.0", "1.17.0", "1.18.1", "1.18.2"]

    for version in mypy_versions:
        print(f"Testing with mypy {version}...")
        venv_dir = setup_mypy_venv(version, results_dir)
        mypy = MypyAdapter(test_dir, version, venv_dir)

        for i, config in enumerate(configs):
            config_desc = "default" if i == 0 else "no-cache"
            result = mypy.run(config)
            all_results.append(result)

            status = "✓ PASSED" if result.passed else f"✗ FAILED ({len(result.errors)} errors)"
            print(f"  {status}: config={config_desc}")

        print()

    # Test with pre-commit isolation
    print("Testing with pre-commit isolation...")
    for version in ["1.18.2"]:  # Test latest version
        precommit_mypy = PreCommitMypyAdapter(test_dir, version)
        for i, config in enumerate(configs):
            config_desc = "default" if i == 0 else "no-cache"
            result = precommit_mypy.run(config)
            all_results.append(result)

            status = "✓ PASSED" if result.passed else f"✗ FAILED ({len(result.errors)} errors)"
            print(f"  {status}: pre-commit mypy {version} config={config_desc}")
    print()

    # Save results as JSON
    results_file = results_dir / "test_results.json"
    with results_file.open("w") as f:
        json.dump([r.to_dict() for r in all_results], f, indent=2)

    print("=" * 70)
    print(f"Results saved to: {results_file}")
    print("=" * 70)
    print()

    # Print summary
    print("Summary:")
    for result in all_results:
        status = "✓" if result.passed else "✗"
        config_str = f"disable_cache={result.config.get('disable_expression_cache', False)}"
        print(f"{status} {result.checker:20s} {result.version:10s} {config_str}")

    # Determine if issue exists
    mypy_118_failures = [r for r in all_results
                         if r.checker == "mypy" and r.version.startswith("1.18") and not r.passed]
    if mypy_118_failures:
        print("\n⚠ Issue confirmed in mypy 1.18+")
    else:
        print("\n✓ No issues detected (or workaround is effective)")


if __name__ == "__main__":
    main()
