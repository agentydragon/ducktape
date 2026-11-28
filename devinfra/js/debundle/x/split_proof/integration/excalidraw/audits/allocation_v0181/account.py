"""Join the pinned Rust allocation, source bundle, and output manifest into audit tables."""

import argparse
import csv
import gzip
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path


def read(path: Path):
    return json.loads(path.read_bytes())


def replay_owners(root: Path, out: Path):
    """Export classification for diagnostic placement owners; no per-owner publication was attempted."""
    fields = ["rows", "executable", "imports", "exports", "concrete", "opaqueFallback", "refused"]
    with (out / "replay.tsv").open("w", newline="") as f:
        writer = csv.writer(f, delimiter="\t", lineterminator="\n")
        writer.writerow(["chunk", "placement", "requested_destination", *fields])
        for chunk in ("editor", "main"):
            inventory = read(root / chunk / "owners.json")
            for placement in ("full", "selected"):
                for owner, counts in sorted(inventory[placement + "Owners"].items()):
                    assert counts["executable"] == sum(counts[k] for k in ("concrete", "opaqueFallback", "refused"))
                    writer.writerow([chunk, placement, owner, *(counts[k] for k in fields)])


def main(root: Path, subset_file: Path, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    placements = {p["chunk_id"]: p for p in read(root / "canonical_deployment_placements.json")["placements"]}
    subset = {(m["chunk"], m["module"].lower()): set(m["bindings"]) for m in read(subset_file)["modules"]}
    matched_subset = set()
    app = root / "debundle.out/app"
    bundle = root / "bundle"
    summary = read(root / "canonical_summary.json")
    graphs = root / "debundle.dependency_graphs/reports/tree"
    roster = {x["chunk_id"]: x["source_path"] for x in read(root / "debundle.out/reports/chunks.json")["chunks"]}
    roster.update(editor="chunk-PN7KOLDJ.js", main="main.js")
    seen_source = set()
    chunks = []
    owner_rows = []
    destination_rows = []
    seen_output = set()
    for report in sorted(graphs.rglob("dependency_graph.json.gz")):
        graph = json.loads(gzip.decompress(report.read_bytes()))
        chunk = graph["chunk_id"]
        nodes = sorted(graph["nodes"], key=lambda n: n["statement_ordinal"])
        assert [n["statement_ordinal"] for n in nodes] == list(range(len(nodes)))
        entries = {m["key"]: m for m in graph["module_graph"]["nodes"]}
        by_destination = defaultdict(list)
        for n in nodes:
            by_destination[n["destination"]].append(n)
        reported = read(report.parent / "modules.json")["final_module_contents"]
        physical = {m["path"]: m for m in reported}
        assert len(physical) == len(reported)
        counts = Counter()
        entry_byte_identical = "-"
        per_owner = defaultdict(Counter)
        known = placements.get(chunk)
        known_units = known["units"] if known else [None] * len(nodes)
        assert len(nodes) == len(known_units)
        src = roster.get(chunk) or (nodes[0]["source_location"]["source_path"] if nodes else "")
        source_file = bundle / src
        source_size = source_file.stat().st_size if source_file.is_file() else "-"
        if source_file.is_file():
            assert src not in seen_source, src
            seen_source.add(src)
        if known:
            assert [unit["ordinal"] for unit in known_units] == list(range(len(nodes)))
            assert hashlib.sha256(source_file.read_bytes()).hexdigest() == known["source_sha256"]
        for dest, loc in entries.items():
            group = by_destination[dest]
            expected = loc.get("path")
            if loc["kind"] == "entry":
                relative = f"{chunk}/entry.js"
                category = "entry"
            else:
                mod = physical[expected]
                assert set(mod["source_unit_ids"]) <= {n["id"] for n in group}, (chunk, expected)
                if not mod["residual"]:
                    assert set(mod["source_unit_ids"]) == {n["id"] for n in group}, (chunk, expected)
                relative = f"{chunk}/{mod['file']}"
                category = "residual_group" if mod["residual"] else "named"
            artifact = app / relative
            file_bytes = "-"
            if relative in summary["emitted"]:
                bs = artifact.read_bytes()
                assert hashlib.sha256(bs).hexdigest() == summary["emitted"][relative], relative
                assert relative not in seen_output
                seen_output.add(relative)
                file_bytes = len(bs)
                counts[category + "_bytes"] += file_bytes
                if category == "entry" and source_file.is_file():
                    entry_byte_identical = int(bs == source_file.read_bytes())
            else:
                assert not artifact.exists(), (chunk, relative)
                assert chunk.startswith("vendor/"), (chunk, relative)
            dc = Counter()
            for n in group:
                counts["source_units"] += 1
                dc["source_units"] += 1
                if n["statement_kind"] in {"import", "export"}:
                    counts["metadata"] += 1
                    dc["metadata"] += 1
                    continue
                counts["executable"] += 1
                dc["executable"] += 1
                oracle = known_units[n["statement_ordinal"]]
                home = oracle and oracle.get("owner")
                selected = set()
                if home:
                    key = (chunk, home)
                    selected = set(oracle["members"].values()) & subset.get(key, set())
                    if selected:
                        assert selected == set(oracle["members"].values()), (chunk, n)
                        for m in selected:
                            member = (chunk, home, m)
                            assert member not in matched_subset
                            matched_subset.add(member)
                        counts["historical_selected_units"] += 1
                        per_owner[home]["historical_selected_units"] += 1
                    per_owner[home]["known_units"] += 1
                    counts["known_units"] += 1
                    dc["known_units"] += 1
                else:
                    counts["unknown_units"] += 1
                    dc["unknown_units"] += 1
                if category == "named":
                    if home == expected:
                        field = "correct_named_units"
                        if selected:
                            counts["historical_selected_correct_named_units"] += 1
                            per_owner[home]["historical_selected_correct_named_units"] += 1
                        exports = {b["binding"]: b.get("export_name", b["binding"]) for b in n["declared_bindings"]}
                        for old, new in oracle["members"].items():
                            field_name = (
                                "reported_renamed_bindings" if exports[old] != old else "reported_unchanged_bindings"
                            )
                            counts[field_name] += 1
                            per_owner[home][field_name] += 1
                            assert exports[old] == new, (chunk, expected, old, new, exports[old])
                    else:
                        field = "wrong_named_units" if home else "unattributed_named_units"
                    if expected:
                        per_owner[expected]["named_output_units"] += 1
                        if field != "correct_named_units":
                            per_owner[expected]["extra_output_units"] += 1
                elif home:
                    field = "retained_known_units"
                else:
                    field = "retained_unknown_units"
                counts[field] += 1
                dc[field] += 1
                if home:
                    per_owner[home][field] += 1
            destination_rows.append(
                {
                    "chunk": chunk,
                    "destination": expected or "(entry)",
                    "file": relative,
                    "kind": category,
                    "file_bytes": file_bytes,
                    **dc,
                }
            )
            if category == "named":
                per_owner[expected]["named_file_bytes"] += file_bytes
        assert counts["source_units"] == counts["executable"] + counts["metadata"]
        assert counts["known_units"] + counts["unknown_units"] == counts["executable"]
        assert (
            sum(
                counts[k]
                for k in (
                    "correct_named_units",
                    "wrong_named_units",
                    "unattributed_named_units",
                    "retained_known_units",
                    "retained_unknown_units",
                )
            )
            == counts["executable"]
        )
        chunks.append(
            {
                "chunk": chunk,
                "source_path": src,
                "source_bytes": source_size,
                "entry_byte_identical": entry_byte_identical,
                **counts,
            }
        )
        for home, c in sorted(per_owner.items()):
            known_units_count = c["known_units"]
            correct = c["correct_named_units"]
            status = (
                "complete_known"
                if correct == known_units_count and not c["extra_output_units"]
                else ("partial_known" if correct else "no_correct_named")
            )
            owner_rows.append({"chunk": chunk, "owner": home, "known_bag_status": status, **c})

    assert matched_subset == {(c, o, b) for (c, o), bindings in subset.items() for b in bindings}, (
        "missing historical members",
        {(c, o, b) for (c, o), bs in subset.items() for b in bs} - matched_subset,
    )
    unreported = []
    for f in sorted(bundle.rglob("*.js")):
        src = f.relative_to(bundle).as_posix()
        if src not in seen_source:
            unreported.append((src, f.stat().st_size))
    for k, digest in sorted(summary["emitted"].items()):
        if k.endswith(".js") and k not in seen_output:
            data = (app / k).read_bytes()
            assert hashlib.sha256(data).hexdigest() == digest
            destination_rows.append(
                {
                    "chunk": "(unjoined)",
                    "destination": "(no graph join)",
                    "file": k,
                    "kind": "unjoined",
                    "file_bytes": len(data),
                }
            )

    def save(name, rows, fields):
        with (out / name).open("w", newline="") as f:
            writer = csv.writer(f, delimiter="\t", lineterminator="\n")
            writer.writerow(fields)
            for row in rows:
                writer.writerow(row.get(k, "-" if row.get("kind") == "unjoined" else 0) for k in fields)

    metrics = [
        "source_units",
        "metadata",
        "executable",
        "known_units",
        "unknown_units",
        "correct_named_units",
        "wrong_named_units",
        "unattributed_named_units",
        "retained_known_units",
        "retained_unknown_units",
    ]
    save(
        "chunks.tsv",
        chunks,
        [
            "chunk",
            "source_path",
            "source_bytes",
            *metrics,
            "historical_selected_units",
            "historical_selected_correct_named_units",
            "reported_renamed_bindings",
            "reported_unchanged_bindings",
            "named_bytes",
            "entry_bytes",
            "residual_group_bytes",
            "entry_byte_identical",
        ],
    )
    save(
        "owners.tsv",
        owner_rows,
        [
            "chunk",
            "owner",
            "known_bag_status",
            "known_units",
            "correct_named_units",
            "retained_known_units",
            "wrong_named_units",
            "named_output_units",
            "extra_output_units",
            "historical_selected_units",
            "historical_selected_correct_named_units",
            "reported_renamed_bindings",
            "reported_unchanged_bindings",
            "named_file_bytes",
        ],
    )
    save("destinations.tsv", destination_rows, ["chunk", "destination", "file", "kind", "file_bytes", *metrics])
    for row in chunks:
        if row["chunk"] in placements:
            print(row)
    print("all graph totals", len(chunks), {k: sum(row.get(k, 0) for row in chunks) for k in metrics})
    print("unjoined source", unreported)
    print("unjoined output", [r for r in destination_rows if r["kind"] == "unjoined"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build_outputs", type=Path)
    parser.add_argument("subset", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--replay-outputs", type=Path, help="authored test outputs directory containing editor/ and main/"
    )
    args = parser.parse_args()
    main(args.build_outputs, args.subset, args.output_dir)
    if args.replay_outputs:
        replay_owners(args.replay_outputs, args.output_dir)
