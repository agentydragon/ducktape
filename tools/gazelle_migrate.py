#!/usr/bin/env python3
"""Migrate Python BUILD files to Gazelle compatibility.

This script prepares BUILD.bazel files for Gazelle by:
1. Adding `# gazelle:exclude setup.py` where needed
2. Adding `# gazelle:python_library_naming_convention` for name conflicts
3. Adding `# keep` comments to preserve manual deps
4. Converting glob() to explicit file lists (optional)

Run: python3 tools/gazelle_migrate.py --dry-run
Then: bazel run //:gazelle
"""

import argparse
import re
from pathlib import Path

# Packages with name conflicts (py_binary has same name as package)
NAME_CONFLICTS = ["finance/reconcile"]

# Patterns that need # keep comments (deps Gazelle might remove)
KEEP_DEPS = ["@pypi//pytest", "@pypi//pytest_asyncio"]


def find_python_builds(root: Path) -> list[Path]:
    """Find all BUILD.bazel files with Python targets."""
    builds = []
    for build in root.rglob("BUILD.bazel"):
        if "bazel-" in str(build):
            continue
        content = build.read_text()
        if "py_library" in content or "py_binary" in content or "py_test" in content:
            builds.append(build)
    return sorted(builds)


def needs_setup_exclude(build_path: Path) -> bool:
    """Check if directory has a setup.py that needs excluding."""
    return (build_path.parent / "setup.py").exists()


def has_name_conflict(build_path: Path, root: Path) -> bool:
    """Check if package has py_binary with same name as directory."""
    rel_path = build_path.parent.relative_to(root)
    return str(rel_path) in NAME_CONFLICTS


def add_directive(content: str, directive: str) -> str:
    """Add a gazelle directive at the top of the file."""
    if directive in content:
        return content
    # Add after any existing gazelle directives or at the very top
    lines = content.split("\n")
    insert_idx = 0
    for i, line in enumerate(lines):
        if line.startswith("# gazelle:"):
            insert_idx = i + 1
        elif line.strip() and not line.startswith("#"):
            break
    lines.insert(insert_idx, directive)
    return "\n".join(lines)


def add_keep_comments(content: str) -> str:
    """Add # keep comments to deps that Gazelle might remove."""
    for dep in KEEP_DEPS:
        # Match the dep line without # keep
        pattern = rf'("{dep}")(,?)(\s*)$'
        replacement = r'"\g<1>",  # keep\g<3>'
        # Only add if not already present
        if dep in content and "# keep" not in content.split(dep)[1].split("\n")[0]:
            content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
    return content


def migrate_build(build_path: Path, root: Path, dry_run: bool) -> list[str]:
    """Migrate a single BUILD.bazel file. Returns list of changes made."""
    changes = []
    content = build_path.read_text()
    original = content

    # Add setup.py exclude if needed
    if needs_setup_exclude(build_path):
        content = add_directive(content, "# gazelle:exclude setup.py")
        changes.append("added setup.py exclude")

    # Add naming convention for name conflicts
    if has_name_conflict(build_path, root):
        content = add_directive(content, "# gazelle:python_library_naming_convention $package_name$_lib")
        changes.append("added naming convention for conflict")

    # Add # keep comments to important deps
    new_content = add_keep_comments(content)
    if new_content != content:
        content = new_content
        changes.append("added # keep comments")

    if content != original:
        if dry_run:
            print(f"\n--- {build_path} ---")
            print(f"Changes: {', '.join(changes)}")
        else:
            build_path.write_text(content)

    return changes


def main():
    parser = argparse.ArgumentParser(description="Migrate BUILD files for Gazelle")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without applying")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Repository root")
    args = parser.parse_args()

    builds = find_python_builds(args.root)
    print(f"Found {len(builds)} Python BUILD.bazel files")

    total_changes = 0
    for build in builds:
        changes = migrate_build(build, args.root, args.dry_run)
        if changes:
            total_changes += 1

    print(f"\n{'Would modify' if args.dry_run else 'Modified'} {total_changes} files")
    if args.dry_run:
        print("\nRun without --dry-run to apply changes, then:")
        print("  bazel run //:gazelle")


if __name__ == "__main__":
    main()
