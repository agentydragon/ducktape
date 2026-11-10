"""Summarize an accepted debundle tree for the canonical-spec check.

Per chunk, the gate and translation-validation verdicts and counts; per emitted
file, its SHA-256. Diffing two summaries shows a new rejection, a changed
translation-validation result, and changed emitted files or bytes.
"""

import argparse
import hashlib
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True, help="The `debundle_pipeline` output tree.")
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()

    reports = args.out_dir / "reports/tree"
    chunks = {}
    for graph_summary in sorted(reports.rglob("graph_summary.json")):
        chunk_dir = graph_summary.parent
        validation = json.loads((chunk_dir / "translation_validation.json").read_text())
        chunks[chunk_dir.relative_to(reports).as_posix()] = {
            "gate_accepts": json.loads(graph_summary.read_text())["gate_accepts"],
            "translation_validation_accepts": validation["accepts"],
            "translation_validation_counts": validation["counts"],
        }
    app = args.out_dir / "app"
    emitted = {
        path.relative_to(app).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(app.rglob("*"))
        if path.is_file()
    }
    args.summary.write_text(json.dumps({"chunks": chunks, "emitted": emitted}, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
