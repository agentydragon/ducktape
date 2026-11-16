"""Run accepted callback contracts and close conservative-boundary controls."""

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

EXPECTED_STDOUT = {"fresh_factory": "42\n", "retained_constructor": "42\n", "persistent_registry": "42\n"}
CONTROL_CASES = {
    "fresh_factory": ("named_callback.js", 0, "42\n", ("callbacks/wrapped", "values/later")),
    "retained_constructor": ("unlisted_arity.js", 0, "42\n", ("callbacks/wrapped", "values/later")),
    "persistent_registry": ("early_emit.js", 1, "ReferenceError", ("callbacks/registration", "values/later")),
}
ACCEPTED_MODULE_DIRS = {"retained_constructor": "accepted_modules", "persistent_registry": "accepted_modules"}


def run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)


def run_tree(
    debundle: Path, fixture: Path, out_root: Path, module_root: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return run(
        [
            str(debundle),
            "run",
            "--tree-config",
            str(fixture / "spec_config.yaml"),
            "--tree-modules",
            str(module_root or fixture / "modules"),
            "--tree-vendor-marks",
            str(fixture / "vendor_marks.yaml"),
            "--tree-source-root",
            str(fixture),
            "--out-root",
            str(out_root),
        ],
        fixture,
    )


def _assert_conflict(
    out_root: Path,
    modules: tuple[str, str],
    result: subprocess.CompletedProcess[str],
    required_cause: str | None = None,
) -> None:
    report = out_root / "reports/tree/main/atomic_group_conflicts.json"
    if not report.is_file():
        raise AssertionError(f"missing atomic-group rejection report for CLI result:\n{result.stderr}")
    conflicts = json.loads(report.read_text())
    if not any(
        all(any(claim.get("module") == module for claim in conflict.get("claims", [])) for module in modules)
        and (required_cause is None or required_cause in conflict.get("causes", []))
        for conflict in conflicts
    ):
        raise AssertionError(f"no atomic-group conflict spans {modules} with cause {required_cause}: {conflicts!r}")


def _assert_cycle_refusal(
    out_root: Path,
    result: subprocess.CompletedProcess[str],
    expected_modules: tuple[str, str],
    expected_witness_fragments: tuple[str, str],
) -> None:
    report = out_root / "reports/tree/main/cycles.json"
    if not report.is_file():
        raise AssertionError(f"missing cycle rejection report for CLI result:\n{result.stderr}")
    cycles = json.loads(report.read_text())
    expected = set(expected_modules)
    if not any(expected <= set(cycle.get("modules", [])) and cycle.get("cut") for cycle in cycles):
        raise AssertionError(f"no blocking cycle spans {expected_modules}: {cycles!r}")
    if not all(fragment in result.stderr for fragment in expected_witness_fragments):
        raise AssertionError(f"missing expected cycle witness {expected_witness_fragments!r}: {result.stderr}")


def _assert_module_exports(node: Path, out_root: Path, module_path: Path, expected: list[str]) -> None:
    if not module_path.is_file():
        available = sorted(str(path.relative_to(out_root)) for path in out_root.rglob("*.js"))
        raise AssertionError(f"missing emitted module {module_path}; available JavaScript files: {available}")
    source = (
        "const module = await import("
        + json.dumps(module_path.resolve().as_uri())
        + ");\nprocess.stdout.write(JSON.stringify(Object.keys(module).sort()));\n"
    )
    result = run([str(node), "--input-type=module", "--eval", source], out_root)
    if result.returncode != 0 or result.stderr:
        raise AssertionError(
            f"could not inspect emitted exports from {module_path}: "
            f"exit={result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}"
        )
    actual = json.loads(result.stdout)
    if actual != sorted(expected):
        raise AssertionError(f"unexpected exports from {module_path}: expected {sorted(expected)}, got {actual}")


def _run_control(debundle: Path, node: Path, fixture: Path, out_root: Path, name: str) -> str:
    control_name, expected_exit, expected_output, modules = CONTROL_CASES[name]
    with tempfile.TemporaryDirectory(prefix="debundle-contract-control-") as tmp:
        control_fixture = Path(tmp) / name
        shutil.copytree(fixture, control_fixture)
        shutil.copyfile(control_fixture / "source/controls" / control_name, control_fixture / "source/main.js")
        baseline = run([str(node), str(control_fixture / "source/main.js")], control_fixture)
        baseline_matches = (
            baseline.returncode == 0 and baseline.stdout == expected_output and not baseline.stderr
            if expected_exit == 0
            else baseline.returncode != 0 and expected_output in baseline.stderr
        )
        if not baseline_matches:
            raise AssertionError(
                f"unexpected Node control baseline for {name}: "
                f"exit={baseline.returncode}, stdout={baseline.stdout!r}, stderr={baseline.stderr!r}"
            )
        output = out_root / "controls" / name
        transformed = run_tree(debundle, control_fixture, output)
        if transformed.returncode == 0:
            raise AssertionError(f"debundle accepted unsupported control {name}")
        if name == "fresh_factory":
            _assert_cycle_refusal(
                output,
                transformed,
                ("callbacks/wrapped", "anon_residual_sentinel"),
                ("wrapped (callbacks/wrapped, unit:", "callback (anon_residual_sentinel, unit:"),
            )
        else:
            _assert_conflict(
                output, modules, transformed, required_cause="eager_use" if name == "persistent_registry" else None
            )
    return baseline.stdout + baseline.stderr


def run_examples(debundle: Path, node: Path, fixtures: Path, out_root: Path) -> dict[str, str]:
    debundle = debundle.resolve()
    node = node.resolve()
    fixtures = fixtures.resolve()
    out_root = out_root.resolve()
    outputs = {}
    out_root.mkdir(parents=True, exist_ok=True)
    for name, expected in EXPECTED_STDOUT.items():
        fixture = fixtures / name
        baseline = run([str(node), str(fixture / "source/main.js")], fixture)
        if baseline.returncode != 0:
            raise RuntimeError(f"Node baseline failed for {name}:\n{baseline.stderr}")
        if baseline.stdout != expected or baseline.stderr:
            raise AssertionError(f"unexpected Node baseline for {name}: {baseline.stdout!r} {baseline.stderr!r}")

        output = out_root / name
        module_root = fixture / ACCEPTED_MODULE_DIRS[name] if name in ACCEPTED_MODULE_DIRS else None
        transformed = run_tree(debundle, fixture, output, module_root)
        if transformed.returncode != 0:
            raise RuntimeError(f"debundle rejected {name}:\n{transformed.stdout}\n{transformed.stderr}")

        emitted = run([str(node), str(output / "app/main/entry.js")], output)
        if emitted.returncode != 0:
            raise RuntimeError(f"Node rejected emitted {name}:\n{emitted.stderr}")
        if emitted.stdout != baseline.stdout or emitted.stderr != baseline.stderr:
            raise AssertionError(
                f"output changed for {name}: baseline={baseline.stdout!r} {baseline.stderr!r}; "
                f"emitted={emitted.stdout!r} {emitted.stderr!r}"
            )
        if name == "retained_constructor":
            _assert_module_exports(node, output, output / "app/main/callbacks/wrapped.js", ["wrapped"])
        elif name == "persistent_registry":
            _assert_module_exports(node, output, output / "app/main/callbacks/registration.js", ["observed"])
        outputs[name] = emitted.stdout

    for name in CONTROL_CASES:
        outputs[f"control:{name}"] = _run_control(debundle, node, fixtures / name, out_root, name)
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--debundle", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    outputs = run_examples(args.debundle, args.node, args.fixtures, args.out)
    for name, stdout in outputs.items():
        if name.startswith("control:"):
            print(f"{name}: Node baseline {stdout!r}; gate rejected the cross-module split")
        else:
            print(f"{name}: preserved stdout {stdout!r}")


if __name__ == "__main__":
    main()
