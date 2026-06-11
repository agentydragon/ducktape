"""Load YAML issue files for sync operation.

⚠️⚠️⚠️ PRIVATE MODULE - DO NOT IMPORT OUTSIDE db/sync/ ⚠️⚠️⚠️
"""

from __future__ import annotations

from pathlib import Path

import yaml

from props.core.ids import SnapshotSlug, split_snapshot_slug
from props.db.sync._models import FalsePositive, TruePositive
from props.db.sync._yaml_models import YAMLIssue


def load_yaml_issues(slug: SnapshotSlug, specimens_dir: Path) -> tuple[list[TruePositive], list[FalsePositive]]:
    """Load YAML issue files for a snapshot."""
    repo, version = split_snapshot_slug(slug)
    snapshot_dir = specimens_dir / repo / version

    if not snapshot_dir.is_dir():
        raise FileNotFoundError(f"Snapshot directory not found: {snapshot_dir}")

    # Discover all YAML files in the issues subdirectory
    issues_dir = snapshot_dir / "issues"
    if not issues_dir.is_dir():
        raise FileNotFoundError(f"Issues directory not found: {issues_dir}")

    yaml_files = sorted(issues_dir.glob("*.yaml"))

    true_positives: list[TruePositive] = []
    false_positives: list[FalsePositive] = []

    for yaml_file in yaml_files:
        issue_id = yaml_file.stem

        with yaml_file.open() as f:
            raw_data = yaml.safe_load(f)

        issue = YAMLIssue.model_validate(raw_data)

        if issue.should_flag:
            true_positives.append(issue.to_true_positive(tp_id=issue_id, snapshot_slug=slug))
        else:
            false_positives.append(issue.to_false_positive(fp_id=issue_id, snapshot_slug=slug))

    return true_positives, false_positives
