"""Lossless, inspectable sharing of repeated recorded-response JSON subtrees."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

_FORMAT = "grafana-recorded-shared-v1"
_REF = "$recorded_ref"


def expand_fixture(document: dict[str, Any]) -> dict[str, Any]:
    """Read ordinary recordings or expand a versioned shared-value recording."""
    if "recorded_format" not in document:
        return document
    if document["recorded_format"] != _FORMAT or set(document) != {"recorded_format", "shared", "fixture"}:
        raise ValueError("unsupported recorded fixture format")
    shared = document["shared"]
    if not isinstance(shared, dict):
        raise ValueError("shared values must be an object")

    def expand(value: Any, active: frozenset[str]) -> Any:
        if isinstance(value, dict):
            if _REF in value:
                name = value[_REF]
                if set(value) != {_REF} or not isinstance(name, str) or name not in shared or name in active:
                    raise ValueError("invalid, missing or cyclic recorded reference")
                # Each use gets an independent object, as in an ordinary JSON parse.
                return expand(shared[name], active | {name})
            return {key: expand(item, active) for key, item in value.items()}
        if isinstance(value, list):
            return [expand(item, active) for item in value]
        return value

    fixture = expand(document["fixture"], frozenset())
    if not isinstance(fixture, dict):
        raise ValueError("recorded fixture must be an object")
    return fixture


def compact_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    """Factor repeated containers without changing any captured scalar or array."""
    counts: Counter[str] = Counter()

    def key(value: Any) -> str:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)

    def count(value: Any) -> None:
        if isinstance(value, dict):
            if _REF in value or "recorded_format" in value:
                raise ValueError("recording contains a reserved codec key")
            for item in value.values():
                count(item)
        elif isinstance(value, list):
            for item in value:
                count(item)
        else:
            return
        counts[key(value)] += 1

    count(fixture)
    names: dict[str, str] = {}
    shared: dict[str, Any] = {}

    def encode(value: Any) -> Any:
        if not isinstance(value, (dict, list)):
            return value
        serialized = key(value)
        # Tiny subtrees cost more references than they save. No scalar is encoded.
        reusable = counts[serialized] > 1 and len(serialized) >= 100
        if reusable and serialized in names:
            return {_REF: names[serialized]}
        if reusable:
            name = f"value_{len(names) + 1:03d}"
            names[serialized] = name
        encoded = (
            {field: encode(item) for field, item in value.items()}
            if isinstance(value, dict)
            else [encode(item) for item in value]
        )
        if reusable:
            shared[name] = encoded
            return {_REF: name}
        return encoded

    encoded = encode(fixture)
    if not shared:
        return fixture
    result = {"recorded_format": _FORMAT, "shared": shared, "fixture": encoded}
    if expand_fixture(result) != fixture:
        raise ValueError("recorded fixture failed lossless conversion")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["compact", "expand"])
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    fixture = expand_fixture(json.loads(args.source.read_text()))
    result = compact_fixture(fixture) if args.operation == "compact" else fixture
    args.destination.write_text(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
