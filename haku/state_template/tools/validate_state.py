#!/usr/bin/env python3
"""Validate every state file the UI backend reads live from HEAD.

The backend parses these at request time (no build step), so a malformed commit — a
YAML typo in an item, a manifest that drifted from the model — only surfaces when the
dashboard 500s or silently renders wrong. This gate validates the repo against the
backend's OWN Pydantic models (``ui/backend/models.py``), the actual read contract, so
model edits and data edits can't drift apart unnoticed in either direction.

Run by ``.forgejo/workflows/validate-state.yaml`` on every push to main. When you add a
surface with its own state files (a board, signals, …), add its glob + model here — a
surface this gate doesn't cover is one a bad commit can silently break.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from models import Item, RunManifest
from pydantic import ValidationError

REPO = Path(__file__).resolve().parent.parent


def check(path: Path, model, inject: dict | None = None) -> str | None:
    """Returns an error string, or None if the file parses and validates."""
    try:
        data = yaml.safe_load(path.read_text())
        if inject:
            data = {**data, **inject}
    except yaml.YAMLError as e:
        return f"{path.relative_to(REPO)}: YAML parse error: {e}"
    try:
        model.model_validate(data)
    except ValidationError as e:
        return f"{path.relative_to(REPO)}: does not match {model.__name__}: {e}"
    return None


def main() -> int:
    targets: list[tuple] = []
    targets += [(p, Item) for p in sorted((REPO / "items").glob("*.yaml"))]
    targets += [(p, RunManifest) for p in sorted((REPO / "runs").glob("*/*.yaml"))]

    errors = [err for entry in targets for err in [check(entry[0], entry[1], *entry[2:])] if err]
    for e in errors:
        print(f"FAIL {e}", file=sys.stderr)
    print(f"validate_state: {len(targets) - len(errors)}/{len(targets)} files valid")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
