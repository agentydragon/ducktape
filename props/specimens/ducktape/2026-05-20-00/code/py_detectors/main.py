from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from py_detectors.registry import FileDetector, run_all
from py_detectors.rules.broad_except_order.rule import find_detections as broad_except_order
from py_detectors.rules.dynamic_attr_probe.rule import find_detections as dynamic_attr_probe
from py_detectors.rules.flatten_nested_guards.rule import find_detections as flatten_nested_guards
from py_detectors.rules.import_aliasing.rule import find_detections as import_aliasing
from py_detectors.rules.imports_inside_def.rule import find_all as imports_inside_def_find_all
from py_detectors.rules.magic_tuple_indices.rule import find_detections as magic_tuple_indices
from py_detectors.rules.optional_string_simplify.rule import find_detections as optional_string_simplify
from py_detectors.rules.pathlike_str_casts.rule import find_detections as pathlike_str_casts
from py_detectors.rules.pydantic_v1_shims.rule import find_detections as pydantic_v1_shims
from py_detectors.rules.swallow_errors.rule import find_detections as swallow_errors
from py_detectors.rules.trivial_alias.rule import find_detections as trivial_alias
from py_detectors.rules.trivial_passthrough.rule import find_detections as trivial_passthrough
from py_detectors.rules.walrus_suggest.rule import find_detections as walrus_suggest

DETECTORS: dict[str, FileDetector] = {
    "broad_except_order": broad_except_order,
    "dynamic_attr_probe": dynamic_attr_probe,
    "flatten_nested_guards": flatten_nested_guards,
    "import_aliasing": import_aliasing,
    "magic_tuple_indices": magic_tuple_indices,
    "optional_string_simplify": optional_string_simplify,
    "pathlike_str_casts": pathlike_str_casts,
    "pydantic_v1_shims": pydantic_v1_shims,
    "swallow_errors": swallow_errors,
    "trivial_alias": trivial_alias,
    "trivial_passthrough": trivial_passthrough,
    "walrus_suggest": walrus_suggest,
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run properties detectors (standalone)")
    ap.add_argument("--root", required=True, type=Path, help="Repo root or path to scan")
    ap.add_argument("--out", type=Path, help="Write JSON to this file (default: stdout)")
    ap.add_argument("--workers", type=int, default=None, help="Detector worker threads (None=auto; 1=sequential)")
    ap.add_argument("--only", action="append", help="Detector name(s) to run (repeatable); default is all")
    args = ap.parse_args(argv)

    root = args.root.resolve()
    only = set(args.only) if args.only else None

    # Per-file detectors
    dets = run_all(DETECTORS, root, detector_names=only, workers=args.workers)

    # Root-level detectors (need full repo context)
    if only is None or "imports_inside_def" in only:
        dets.extend(imports_inside_def_find_all(root))

    payload: list[dict[str, Any]] = [d.model_dump(exclude_none=True, mode="json") for d in dets]
    s = json.dumps(payload, indent=2)
    if args.out:
        args.out.write_text(s, encoding="utf-8")
    else:
        print(s)
    print(f"[detectors] ran {len(DETECTORS) + 1} detectors; findings: {len(dets)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
