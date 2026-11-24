"""Keep unselected units in entry and restrict import priority to the selected owners."""

import argparse
import json
from pathlib import Path

import yaml


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("subset", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    config = yaml.safe_load(args.config.read_text())
    modules = json.loads(args.subset.read_text())["modules"]
    for chunk, order in config["import_order"].items():
        selected = {owner["module"] for owner in modules if owner["chunk"] == chunk}
        if missing := selected.difference(order):
            raise ValueError(f"{chunk}: owners absent from the canonical import priority: {sorted(missing)}")
        config["import_order"][chunk] = [owner for owner in order if owner in selected]
        config["unassigned_mode"][chunk] = {"kind": "inline_in_entry"}
    args.out.write_text(yaml.safe_dump(config, sort_keys=False))


if __name__ == "__main__":
    main()
