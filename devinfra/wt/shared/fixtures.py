from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, TypeAdapter, ValidationError

from wt.shared.configuration import Configuration
from wt.shared.github_models import PRData, PRState


class PRFixtureEntry(BaseModel):
    number: int
    state: PRState = PRState.OPEN
    draft: bool = False
    mergeable: bool = True
    merged_at: str | None = None
    additions: int = 0
    deletions: int = 0


def write_pr_fixtures_file(config: Configuration, fixtures: Mapping[str, PRFixtureEntry | dict]) -> Path:
    """Validate and write PR fixtures to $WT_DIR/pr_fixtures.json.

    Tests can call this directly or via a pytest fixture wrapper.
    """
    validated: dict[str, PRFixtureEntry] = {
        k: (v if isinstance(v, PRFixtureEntry) else PRFixtureEntry.model_validate(v)) for k, v in fixtures.items()
    }
    path = config.wt_dir / "pr_fixtures.json"
    # Serialize via Pydantic TypeAdapter for stable ordering/shape under tests
    content_bytes = TypeAdapter(dict[str, PRFixtureEntry]).dump_json(validated, by_alias=False)
    path.write_bytes(content_bytes)
    return path


def load_pr_fixture(config: Configuration, branch_name: str) -> PRData | None:
    """Load a single PRData for the given branch from $WT_DIR/pr_fixtures.json.

    Supports per-branch entries or a catch-all "*" entry. Returns None if no match.
    """
    path = config.wt_dir / "pr_fixtures.json"
    if not path.exists():
        return None
    adapter = TypeAdapter(dict[str, PRFixtureEntry])
    try:
        fixtures = adapter.validate_json(path.read_text())
    except (ValidationError, json.JSONDecodeError, OSError):
        return None
    entry = fixtures.get(branch_name) or fixtures.get("*")
    if not entry:
        return None
    return PRData(
        pr_number=entry.number,
        pr_state=entry.state,
        draft=entry.draft,
        mergeable=entry.mergeable,
        merged_at=entry.merged_at,
        additions=entry.additions,
        deletions=entry.deletions,
    )
