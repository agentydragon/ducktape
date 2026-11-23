from __future__ import annotations

import ast
import os
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from devinfra.lint.x.py_detectors.models import Detection, LineRange
from devinfra.lint.x.py_detectors.utils import iter_py_files, parse_python_file, read_snippet

# Per-file detector: receives a parsed file and yields detections.
FileDetector = Callable[[Path, ast.AST, str], Iterable[Detection]]


def detection(
    detector: str, prop: str, path: Path, start_line: int, end_line: int | None, message: str, confidence: float = 0.9
) -> Detection:
    """Create a Detection with auto-generated snippet."""
    return Detection(
        property=prop,
        path=path,
        ranges=[LineRange(start_line=start_line, end_line=end_line)],
        detector=detector,
        confidence=confidence,
        message=message,
        snippet=read_snippet(path, start_line, end_line, context=0),
    )


def run_all(
    detectors: dict[str, FileDetector],
    root: Path,
    detector_names: Iterable[str] | None = None,
    *,
    workers: int | None = None,
) -> list[Detection]:
    """Run all (or selected) detectors.

    Concurrency is controlled by a single flag:
    - workers is None: choose a default pool size = min(len(selected), cpu_count).
    - workers <= 1: run sequentially.
    - workers > 1: run with a thread pool of given size.
    """
    root = root.resolve()
    wanted = set(detector_names) if detector_names is not None else set()
    selected = {name: fn for name, fn in detectors.items() if not wanted or name in wanted}
    if not selected:
        return []

    def scan(path: Path) -> list[Detection]:
        parsed = parse_python_file(path)
        if parsed is None:
            return []
        tree, source = parsed
        findings: list[Detection] = []
        for name, detector in selected.items():
            try:
                findings.extend(detector(path, tree, source))
            except Exception as error:
                findings.append(
                    Detection(
                        property="unknown",
                        path=path,
                        ranges=[],
                        detector=name,
                        confidence=0.1,
                        message=f"detector error: {error}",
                    )
                )
        return findings

    paths = iter_py_files(root)
    count = min(len(selected), os.cpu_count() or 1) if workers is None else workers
    if count <= 1:
        findings = [item for path in paths for item in scan(path)]
    else:
        with ThreadPoolExecutor(max_workers=count) as executor:
            findings = [item for batch in executor.map(scan, paths, buffersize=count * 2) for item in batch]
    # Keep CLI reports grouped in rule order regardless of worker count.
    order = {name: index for index, name in enumerate(selected)}
    return sorted(findings, key=lambda item: order[item.detector])
