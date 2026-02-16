"""Merge issue YAMLs and wrap with split field."""

import sys
from pathlib import Path

import yaml

# Import SpecimenData from sync.py for validation
# This ensures consistent structure between write (here) and read (sync_specimen_from_bundle)
from props.db.sync.sync import SpecimenData


def main():
    output = Path(sys.argv[1])
    split = sys.argv[2]
    issue_files = sys.argv[3:]

    # Merge all issue YAMLs into a dict keyed by issue_id (file stem)
    merged_issues = {}
    for issue_file in issue_files:
        if not issue_file:
            raise ValueError("Empty string in issue_files list - this should not happen")

        issue_path = Path(issue_file)
        issue_id = issue_path.stem

        # Check for duplicate issue IDs
        if issue_id in merged_issues:
            raise ValueError(f"Duplicate issue ID: {issue_id}")

        with issue_path.open() as f:
            issue_data = yaml.safe_load(f)
            if not issue_data:
                raise ValueError(f"Empty or invalid YAML in {issue_file}")
            merged_issues[issue_id] = issue_data

    # Create and validate SpecimenData model
    specimen_data = SpecimenData(split=split, issues=merged_issues)

    # Write structured YAML using model_dump(mode='json') to serialize enums
    with output.open("w") as f:
        yaml.safe_dump(specimen_data.model_dump(mode="json"), f, default_flow_style=False, sort_keys=False)


if __name__ == "__main__":
    main()
