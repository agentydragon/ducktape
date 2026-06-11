from __future__ import annotations

import ast
import os
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from py_detectors.models import Detection, LineRange
from py_detectors.utils import iter_py_files, parse_python_file, read_snippet

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

    def _run_detector(name: str, fn: FileDetector) -> list[Detection]:
        try:
            out: list[Detection] = []
            for path in iter_py_files(root):
                if parsed := parse_python_file(path):
                    tree, source = parsed
                    out.extend(fn(path, tree, source))
            return out
        except Exception as e:
            return [
                Detection(
                    property="unknown",
                    path=root,
                    ranges=[],
                    detector=name,
                    confidence=0.1,
                    message=f"detector error: {e}",
                )
            ]

    auto_workers = min(len(selected), os.cpu_count() or 1) if workers is None else int(workers)

    if auto_workers <= 1:
        out: list[Detection] = []
        for name, fn in selected.items():
            out.extend(_run_detector(name, fn))
        return out

    results: list[Detection] = []
    items = list(selected.items())
    with ThreadPoolExecutor(max_workers=auto_workers) as ex:
        for detections in ex.map(lambda item: _run_detector(item[0], item[1]), items):
            results.extend(detections)
    return results
