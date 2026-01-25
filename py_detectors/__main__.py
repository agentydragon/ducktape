from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

# Import detectors to register them
# Ruff covers: none/boolean literal comparisons and some early-bailout/return patterns
from py_detectors import (
    det_broad_except_order,  # noqa: F401
    det_dynamic_attr_probe,  # noqa: F401
    det_flatten_nested_guards,  # noqa: F401
    det_import_aliasing,  # noqa: F401
    det_imports_inside_def,  # noqa: F401
    det_magic_tuple_indices,  # noqa: F401
    det_optional_string_simplify,  # noqa: F401
    det_pathlike_str_casts,  # noqa: F401
    det_pydantic_v1_shims,  # noqa: F401
    det_swallow_errors,  # noqa: F401
    det_trivial_alias,  # noqa: F401
    det_trivial_passthrough,  # noqa: F401
    det_walrus_suggest,  # noqa: F401
)
from py_detectors.registry import all_detectors, run_all


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run properties detectors (standalone)")
    ap.add_argument("--root", required=True, type=Path, help="Repo root or path to scan")
    ap.add_argument("--out", type=Path, help="Write JSON to this file (default: stdout)")
    # Vulture adapter removed: prefer running Vulture directly when needed
    ap.add_argument("--workers", type=int, default=None, help="Detector worker threads (None=auto; 1=sequential)")
    ap.add_argument("--only", action="append", help="Detector name(s) to run (repeatable); default is all")
    args = ap.parse_args(argv)

    root = args.root.resolve()
    dets = run_all(root, detector_names=args.only, workers=args.workers)
    payload: list[dict[str, Any]] = [d.model_dump(exclude_none=True, mode="json") for d in dets]
    s = json.dumps(payload, indent=2)
    if args.out:
        args.out.write_text(s, encoding="utf-8")
    else:
        print(s)
    # Print a tiny summary to stderr-esque (keep exit 0)
    print(f"[detectors] ran {len(all_detectors())} detectors; findings: {len(dets)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
