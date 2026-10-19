"""Print changed YAML paths without printing machine-configuration values."""

import argparse
from pathlib import Path
from typing import Any

import yaml


def changed_paths(before: Any, after: Any, path: str) -> list[str]:
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        return [
            changed
            for key in sorted(before.keys() | after.keys())
            for changed in (
                changed_paths(before[key], after[key], f"{path}.{key}")
                if key in before and key in after
                else [f"{path}.{key}"]
            )
        ]
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        return [
            changed
            for index, (old, new) in enumerate(zip(before, after, strict=True))
            for changed in changed_paths(old, new, f"{path}[{index}]")
        ]
    return [path]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument(
        "--canary-output", type=Path, help="Write the live configuration with only the three reviewed canary changes"
    )
    args = parser.parse_args()
    before = list(yaml.safe_load_all(args.before.read_text()))
    after = list(yaml.safe_load_all(args.after.read_text()))
    if args.canary_output is not None:
        machine = before[0]["machine"]
        desired = after[0]["machine"]
        machine["install"]["image"] = desired["install"]["image"]
        machine.setdefault("kernel", {})["modules"] = desired["kernel"]["modules"]
        label = "storage.allegedly.works/drbd-canary"
        machine.setdefault("nodeLabels", {})[label] = desired["nodeLabels"][label]
        with args.canary_output.open("w") as output:
            yaml.safe_dump_all(before, output, sort_keys=False)
        return
    for path in changed_paths(before, after, "documents"):
        print(path)


if __name__ == "__main__":
    main()
