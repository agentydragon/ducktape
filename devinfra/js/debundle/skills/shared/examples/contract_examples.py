"""Run accepted callback contracts and close conservative-boundary controls."""

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

EXPECTED_STDOUT = "42\n"


class Control(NamedTuple):
    source: str
    callback_module: str
    node_error: str | None = None
    required_cause: str | None = None


CONTROLS = {
    "fresh_factory": Control("named_callback.js", "callbacks/wrapped"),
    "retained_constructor": Control("unlisted_arity.js", "callbacks/wrapped"),
    "persistent_registry": Control(
        "early_emit.js", "callbacks/registration", node_error="ReferenceError", required_cause="eager_use"
    ),
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


def _assert_conflict(out_root: Path, control: Control, result: subprocess.CompletedProcess[str]) -> None:
    report = out_root / "reports/tree/main/atomic_group_conflicts.json"
    if not report.is_file():
        raise AssertionError(f"missing atomic-group rejection report for CLI result:\n{result.stderr}")
    conflicts = json.loads(report.read_text())
    modules = {control.callback_module, "values/later"}
    for conflict in conflicts:
        if modules <= {claim.get("module") for claim in conflict.get("claims", [])} and (
            control.required_cause is None or control.required_cause in conflict.get("causes", [])
        ):
            return
    raise AssertionError(f"no atomic-group conflict spans {modules} with cause {control.required_cause}: {conflicts!r}")


def node_baseline(node: Path, fixture: Path, expected_error: str | None = None) -> subprocess.CompletedProcess[str]:
    result = run([str(node), str(fixture / "source/main.js")], fixture)
    if expected_error is None:
        if result.returncode == 0 and result.stdout == EXPECTED_STDOUT and not result.stderr:
            return result
    elif result.returncode != 0 and expected_error in result.stderr:
        return result
    raise AssertionError(
        f"unexpected Node baseline for {fixture}: exit={result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}"
    )


def _run_control(debundle: Path, node: Path, fixture: Path, out_root: Path, name: str) -> str:
    control = CONTROLS[name]
    with tempfile.TemporaryDirectory(prefix="debundle-contract-control-") as tmp:
        control_fixture = Path(tmp) / name
        shutil.copytree(fixture, control_fixture)
        shutil.copyfile(control_fixture / "source/controls" / control.source, control_fixture / "source/main.js")
        baseline = node_baseline(node, control_fixture, control.node_error)
        output = out_root / "controls" / name
        transformed = run_tree(debundle, control_fixture, output)
        if transformed.returncode == 0:
            raise AssertionError(f"debundle accepted unsupported control {name}")
        _assert_conflict(output, control, transformed)
    return baseline.stdout + baseline.stderr


def run_examples(debundle: Path, node: Path, fixtures: Path, out_root: Path) -> dict[str, str]:
    debundle = debundle.resolve()
    node = node.resolve()
    fixtures = fixtures.resolve()
    out_root = out_root.resolve()
    outputs = {}
    out_root.mkdir(parents=True, exist_ok=True)
    for name in CONTROLS:
        fixture = fixtures / name
        baseline = node_baseline(node, fixture)

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

    for name in CONTROLS:
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
