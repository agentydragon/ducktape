import os
import sys
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

PRESERVE_KEYS = ("projects", "notice", "windows")


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    raw = path.read_text()
    return tomllib.loads(raw) if raw.strip() else {}


def deep_merge(dst: dict, src: dict) -> dict:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            deep_merge(dst[key], value)
        else:
            dst[key] = value
    return dst


def unmanaged_live_paths(
    live: dict, base: dict, *, prefix: tuple[str, ...] = (), preserve_keys: tuple[str, ...] = PRESERVE_KEYS
) -> list[tuple[str, ...]]:
    paths = []
    for key in sorted(live):
        path = (*prefix, key)
        if (not prefix and key in preserve_keys) or key not in base:
            paths.append(path)
        elif isinstance(live[key], dict) and isinstance(base[key], dict):
            paths.extend(unmanaged_live_paths(live[key], base[key], prefix=path, preserve_keys=preserve_keys))
    return paths


def path_value(doc: dict, path: tuple[str, ...]) -> Any:
    value: Any = doc
    for key in path:
        value = value[key]
    return value


def set_path(doc: dict, path: tuple[str, ...], value: Any) -> None:
    cursor: dict = doc
    for key in path[:-1]:
        cursor = cursor.setdefault(key, {})
    cursor[path[-1]] = value


def unmanaged_live_doc(live: dict, base: dict) -> dict:
    doc: dict[str, Any] = {}
    for path in unmanaged_live_paths(live, base):
        set_path(doc, path, path_value(live, path))
    return doc


def print_unmanaged_live_doc(doc: dict) -> None:
    if not doc:
        return

    print("codex config merge: preserved unmanaged live config TOML:", file=sys.stderr)
    print(tomli_w.dumps(doc).rstrip(), file=sys.stderr)


def main() -> None:
    base = Path(os.environ["BASE"])
    live = Path(os.environ["LIVE"])

    base_doc = load(base)
    if not base_doc:
        raise SystemExit(0)

    live_doc = load(live)
    print_unmanaged_live_doc(unmanaged_live_doc(live_doc, base_doc))

    preserved = {}
    for key in PRESERVE_KEYS:
        if key in live_doc:
            preserved[key] = live_doc.pop(key)

    merged = deep_merge(live_doc, base_doc)
    merged.update(preserved)

    tmp = live.with_suffix(".tmp")
    tmp.write_text(tomli_w.dumps(merged))
    tmp.replace(live)


if __name__ == "__main__":
    main()
