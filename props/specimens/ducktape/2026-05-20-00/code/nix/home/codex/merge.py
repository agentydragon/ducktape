import os
import tomllib
from pathlib import Path

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


def main() -> None:
    base = Path(os.environ["BASE"])
    live = Path(os.environ["LIVE"])

    base_doc = load(base)
    if not base_doc:
        raise SystemExit(0)

    live_doc = load(live)
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
