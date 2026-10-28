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


def run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)


def run_tree(debundle: Path, fixture: Path, out_root: Path) -> subprocess.CompletedProcess[str]:
    return run(
        [
            str(debundle),
            "run",
            "--tree-config",
            str(fixture / "spec_config.yaml"),
            "--tree-modules",
            str(fixture / "modules"),
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
        transformed = run_tree(debundle, fixture, output)
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
