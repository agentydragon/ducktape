#!/usr/bin/env python3
"""Validate every state file the UI backend reads live from HEAD.

The backend parses these at request time (no build step), so a malformed commit — a
YAML typo in an item's frontmatter, a manifest that drifted from the model — only
surfaces when the dashboard 500s or silently renders wrong. This gate validates the repo
against the backend's OWN Pydantic models (``ui/backend/models.py``), the actual read
contract, so model edits and data edits can't drift apart unnoticed in either direction.

Run by ``.forgejo/workflows/validate-state.yaml`` on every push to main. When you add a
surface with its own state files, add its glob + model here — a surface this gate
doesn't cover is one a bad commit can silently break.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml
from models import ImprovementDoc, ItemDoc, ResponseDoc, RunManifest
from pydantic import ValidationError

REPO = Path(__file__).resolve().parent.parent

_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def check(path: Path, model, inject: dict | None = None) -> str | None:
    """Validate a whole-file YAML doc against ``model``; None if it parses and validates."""
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


def check_frontmatter(path: Path, model) -> str | None:
    """Validate a content-collection file's YAML frontmatter against ``model`` (the body is prose,
    rendered client-side). Mirrors the widget's read: a leading ``---`` fenced block."""
    m = _FRONTMATTER.match(path.read_text())
    if not m:
        return f"{path.relative_to(REPO)}: missing YAML frontmatter"
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        return f"{path.relative_to(REPO)}: frontmatter parse error: {e}"
    try:
        model.model_validate(data)
    except ValidationError as e:
        return f"{path.relative_to(REPO)}: frontmatter does not match {model.__name__}: {e}"
    return None


def main() -> int:
    # Whole-file YAML docs: run manifests + operator-answer slots (scope/field are the path).
    targets: list[tuple] = [(p, RunManifest) for p in sorted((REPO / "runs").glob("*/*.yaml"))]
    targets += [(p, ResponseDoc) for p in sorted((REPO / "responses").glob("*/*.yaml"))]

    # Content collections (markdown + typed frontmatter), validated by their `kind`'s model.
    # Skip a collection's own README.md (prose, no frontmatter).
    frontmatter_targets = [(p, ItemDoc) for p in sorted((REPO / "items").glob("*.md")) if p.name != "README.md"]
    frontmatter_targets += [(p, ImprovementDoc) for p in sorted((REPO / "memory/improvements").glob("*.md"))]

    errors = [err for entry in targets for err in [check(entry[0], entry[1], *entry[2:])] if err]
    errors += [err for p, model in frontmatter_targets for err in [check_frontmatter(p, model)] if err]
    total = len(targets) + len(frontmatter_targets)
    for e in errors:
        print(f"FAIL {e}", file=sys.stderr)
    print(f"validate_state: {total - len(errors)}/{total} files valid")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
