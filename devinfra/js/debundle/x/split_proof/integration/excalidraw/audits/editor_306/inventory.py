"""Reconstruct one measured proposal or list all source units and their observed trials."""

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path


def read_pinned(path: Path, digest: str) -> dict:
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError(f"{path}: digest does not match the measured export")
    export = json.loads(data)
    if not isinstance(export, dict):
        raise ValueError(f"{path}: expected a placement object")
    return export


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("canonical", type=Path)
    parser.add_argument("selected", type=Path)
    parser.add_argument("--record", type=Path, default=Path(__file__).with_name("trials.json"))
    parser.add_argument("--trial", help="write just this trial's resolved placement (default: inventory)")
    args = parser.parse_args()
    record = json.loads(args.record.read_bytes())
    canon = read_pinned(args.canonical, record["canonical_sha256"])
    base = read_pinned(args.selected, record["selected_sha256"])
    if not (canon["source_sha256"] == base["source_sha256"] == record["source_sha256"]):
        raise ValueError("source fingerprints disagree")
    if canon["chunk_id"] != base["chunk_id"]:
        raise ValueError("chunk IDs disagree")

    names = {}
    for i, (u, b) in enumerate(zip(canon["units"], base["units"], strict=True)):
        if i != u["ordinal"] or i != b["ordinal"] or (b["owner"] is not None and u != b):
            raise ValueError(f"unit {i}: inconsistent canonical and selected views")
        for name in u.get("members", {}).values():
            key = (u["owner"], name)
            if key in names:
                raise ValueError(f"repeated member {key}")
            names[key] = i

    attempted = defaultdict(list)
    added_by_trial = {}
    for label, trial in record["trials"].items():
        ids = {names[owner, name] for owner, members in trial["add"].items() for name in members}
        for i in ids:
            if base["units"][i]["owner"] is not None:
                raise ValueError(f"{label}: unit {i} is already selected")
            attempted[i].append(label)
        added_by_trial[label] = ids

    if args.trial:
        add = added_by_trial[args.trial]
        units = [u if i in add else base["units"][i] for i, u in enumerate(canon["units"])]
        owners = {u["owner"] for u in units}
        order = [p for p in canon["import_order"] if p in owners]
        json.dump({**base, "units": units, "import_order": order}, sys.stdout)
        return

    for i, u in enumerate(canon["units"]):
        labels = attempted[i]
        if u["owner"] is None:
            status = "unknown_source_owner"
        elif base["units"][i]["owner"] is not None:
            status = "baseline_certified"
        elif any(record["trials"][label]["status"] == "certified" for label in labels):
            status = "in_certified_trial"
        elif labels:
            status = "in_refused_trials_only"
        else:
            status = "untested_on_baseline"
        print(json.dumps({**u, "status": status, "trials": labels}))


if __name__ == "__main__":
    main()
