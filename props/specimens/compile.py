"""Compile specimen artifacts: code tar and data blob."""

import argparse
import tarfile
from pathlib import Path

import yaml

from props.db.sync.sync import SpecimenData


def _compile_code_tar(args: argparse.Namespace) -> None:
    files_to_add: dict[Path, Path] = {}  # arcname -> src_path
    for src_path in args.sources:
        if not src_path.exists():
            raise FileNotFoundError(f"Source file does not exist: {src_path}")

        rel_path = src_path.relative_to(args.strip_prefix)
        if not rel_path.parts:
            raise ValueError(f"Source {src_path} resolved to empty path after stripping {args.strip_prefix}")

        # Restore original filenames from .specimen renames
        if src_path.suffix == ".specimen":
            rel_path = rel_path.with_name(rel_path.stem)

        files_to_add[rel_path] = src_path

    with tarfile.open(args.output, "w") as tar:
        for arcname in sorted(files_to_add.keys(), key=str):
            src_path = files_to_add[arcname]
            tarinfo = tar.gettarinfo(str(src_path), arcname=str(arcname))
            tarinfo.mtime = 0
            tarinfo.uid = 0
            tarinfo.gid = 0
            tarinfo.uname = ""
            tarinfo.gname = ""
            with src_path.open("rb") as f:
                tar.addfile(tarinfo, f)


def _compile_data_blob(args: argparse.Namespace) -> None:
    merged_issues = {}
    for issue_file in args.issue_files:
        if not issue_file:
            raise ValueError("Empty string in issue_files list")

        issue_path = Path(issue_file)
        issue_id = issue_path.stem

        if issue_id in merged_issues:
            raise ValueError(f"Duplicate issue ID: {issue_id}")

        with issue_path.open() as f:
            issue_data = yaml.safe_load(f)
            if not issue_data:
                raise ValueError(f"Empty or invalid YAML in {issue_file}")
            merged_issues[issue_id] = issue_data

    specimen_data = SpecimenData(snapshot_slug=args.slug, split=args.split, issues=merged_issues)

    with args.output.open("w") as f:
        yaml.safe_dump(specimen_data.model_dump(mode="json"), f, default_flow_style=False, sort_keys=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Compile specimen artifacts")
    sub = parser.add_subparsers(dest="command", required=True)

    code_tar = sub.add_parser("code-tar")
    code_tar.add_argument("output", type=Path)
    code_tar.add_argument("--strip-prefix", required=True, type=Path, dest="strip_prefix")
    code_tar.add_argument("sources", nargs="*", type=Path)

    data_blob = sub.add_parser("data-blob")
    data_blob.add_argument("output", type=Path)
    data_blob.add_argument("slug")
    data_blob.add_argument("split")
    data_blob.add_argument("issue_files", nargs="*")

    args = parser.parse_args()
    if args.command == "code-tar":
        _compile_code_tar(args)
    elif args.command == "data-blob":
        _compile_data_blob(args)


if __name__ == "__main__":
    main()
