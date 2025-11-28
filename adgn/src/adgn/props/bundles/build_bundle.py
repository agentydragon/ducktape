#!/usr/bin/env python3
"""Build a git bundle with filtered specimen snapshots.

Reads specimen manifests, applies their bundle filters, and creates a git bundle
containing only the necessary files for each specimen.
"""

import fnmatch
from pathlib import Path
import subprocess

# Import specimen models
import sys
import tempfile

from pydantic import TypeAdapter
import pygit2
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from props.models.specimen import SpecimenDoc


def apply_gitignore_patterns(file_list: list[str], include: list[str] | None, exclude: list[str] | None) -> list[str]:
    """Apply gitignore-style include/exclude patterns to a file list.

    Args:
        file_list: List of file paths
        include: Include patterns (if specified, only matching files are kept)
        exclude: Exclude patterns (matching files are removed)

    Returns:
        Filtered list of file paths
    """

    def matches_pattern(path: str, pattern: str) -> bool:
        """Check if path matches gitignore-style pattern."""
        # Remove trailing slash from pattern (indicates directory)
        if pattern.endswith("/"):
            pattern = pattern.rstrip("/")
            # For directory patterns, match the directory and everything under it
            return path.startswith(pattern + "/") or path == pattern
        # For file patterns, use fnmatch
        return fnmatch.fnmatch(path, pattern) or path.startswith(pattern + "/")

    result = file_list

    # Apply include patterns (if specified, only keep matching files)
    if include:
        result = [f for f in result if any(matches_pattern(f, pattern) for pattern in include)]

    # Apply exclude patterns (remove matching files)
    if exclude:
        result = [f for f in result if not any(matches_pattern(f, pattern) for pattern in exclude)]

    return result


def get_tree_files(repo: pygit2.Repository, tree: pygit2.Tree, prefix: str = "") -> dict[str, tuple]:
    """Get all files in a tree recursively.

    Returns:
        Dict mapping path -> (oid, filemode) tuple
    """
    files = {}

    for entry in tree:
        path = f"{prefix}{entry.name}"
        if entry.type_str == "tree":
            # Recursively walk subtrees
            subtree = repo[entry.id]
            files.update(get_tree_files(repo, subtree, path + "/"))
        else:
            # Store file entry
            files[path] = (entry.id, entry.filemode)

    return files


def copy_blob(source_repo: pygit2.Repository, bundle_repo: pygit2.Repository, oid: pygit2.Oid) -> None:
    """Copy a blob object from source to bundle repo."""
    try:
        # Check if object already exists
        bundle_repo[oid]
        return
    except KeyError:
        pass

    # Get blob from source and write to bundle
    blob = source_repo[oid]
    bundle_repo.write(pygit2.GIT_OBJECT_BLOB, blob.read_raw())


def create_filtered_tree(
    source_repo: pygit2.Repository,
    bundle_repo: pygit2.Repository,
    source_tree: pygit2.Tree,
    include: list[str] | None,
    exclude: list[str] | None,
) -> pygit2.Oid:
    """Create a filtered tree by applying include/exclude patterns.

    Args:
        source_repo: Source repository
        bundle_repo: Bundle repository
        source_tree: Source tree to filter
        include: Include patterns
        exclude: Exclude patterns

    Returns:
        OID of the new filtered tree
    """
    # Get all files from source tree
    all_files = get_tree_files(source_repo, source_tree)

    # Apply filters
    filtered_paths = apply_gitignore_patterns(list(all_files.keys()), include, exclude)
    filtered_files = {path: all_files[path] for path in filtered_paths}

    # Copy necessary blobs to bundle repo
    for oid, _ in filtered_files.values():
        copy_blob(source_repo, bundle_repo, oid)

    # Build tree structure from filtered files
    def build_tree(path_prefix: str) -> pygit2.Oid:
        """Build a tree for a given path prefix."""
        builder = bundle_repo.TreeBuilder()

        # Collect items at this level
        items: dict[str, tuple[str, pygit2.Oid, int]] = {}  # name -> (type, oid, mode)

        for path, (oid, mode) in filtered_files.items():
            if not path.startswith(path_prefix):
                continue

            rel_path = path[len(path_prefix) :]
            if "/" not in rel_path:
                # Direct child (file)
                items[rel_path] = ("blob", oid, mode)
            else:
                # Subdirectory
                dir_name = rel_path.split("/")[0]
                if dir_name not in items:
                    items[dir_name] = ("tree", None, pygit2.GIT_FILEMODE_TREE)

        # Build tree
        for name in sorted(items.keys()):
            item_type, oid, mode = items[name]
            if item_type == "tree":
                # Recursively build subdirectory
                subtree_oid = build_tree(f"{path_prefix}{name}/")
                builder.insert(name, subtree_oid, mode)
            else:
                # Add file
                builder.insert(name, oid, mode)

        return builder.write()

    return build_tree("")


def create_filtered_commit(
    source_repo: pygit2.Repository,
    bundle_repo: pygit2.Repository,
    source_commit_sha: str,
    tag_name: str,
    base_commit: pygit2.Commit,
    include: list[str] | None,
    exclude: list[str] | None,
) -> pygit2.Oid:
    """Create a filtered commit in the bundle repo.

    Args:
        source_repo: Source repository
        bundle_repo: Bundle repository
        source_commit_sha: Source commit SHA
        tag_name: Tag name for the filtered commit
        base_commit: Base commit in bundle repo
        include: Include patterns
        exclude: Exclude patterns

    Returns:
        OID of the created commit
    """
    print(f"Processing {tag_name} from {source_commit_sha}...")

    # Get source commit
    source_commit = source_repo.get(source_commit_sha)
    source_tree = source_commit.tree

    # Get all files and calculate size
    all_files = get_tree_files(source_repo, source_tree)
    orig_size = sum(source_repo[oid].size for oid, _ in all_files.values())

    # Create filtered tree
    filtered_tree_oid = create_filtered_tree(source_repo, bundle_repo, source_tree, include, exclude)
    filtered_tree = bundle_repo[filtered_tree_oid]

    # Calculate filtered size
    filtered_files = get_tree_files(bundle_repo, filtered_tree)
    new_size = sum(bundle_repo[oid].size for oid, _ in filtered_files.values())

    print(f"  Files: {len(all_files)} -> {len(filtered_files)} after filtering")
    print(f"  Original: {orig_size / 1024 / 1024:.1f}MB, Filtered: {new_size / 1024 / 1024:.1f}MB")

    # Create commit with original metadata
    author = source_commit.author
    committer = source_commit.committer
    message = source_commit.message

    new_commit_oid = bundle_repo.create_commit(
        None,  # Don't update any reference
        author,
        committer,
        message,
        filtered_tree_oid,
        [base_commit.id],
    )

    # Create tag
    bundle_repo.create_reference(f"refs/tags/{tag_name}", new_commit_oid)

    print(f"  -> {new_commit_oid}")
    print()

    return new_commit_oid


def main():
    """Build specimen bundle with per-specimen filters."""
    # Configuration
    specimens_dir = Path(__file__).parent.parent / "specimens"
    source_repo_path = Path("/code/gitlab.com/agentydragon/ducktape")
    output_bundle = specimens_dir / "ducktape" / "specimens.bundle"

    # Open source repository
    source_repo = pygit2.Repository(str(source_repo_path))

    # Specimens to include (determined from git tags in source repo)
    # spec_id is relative to specimens_dir (e.g., "ducktape/2025-11-20-adgn")
    specimen_configs = [
        ("ducktape/2025-11-20-adgn", "b729b362de957d127d1e8ac17d8811665ce805fe"),
        ("ducktape/2025-11-20-repo", "c18279d2c1716008afc17a12350aaee89248c1ca"),
        ("ducktape/2025-11-21-repo", "167e39012aca16381442c53bf10d5fc980d0f26a"),
        ("ducktape/2025-11-22-post-fixes", "9395ba65dcef5057176c890981fa0e973d9798eb"),
        ("ducktape/2025-11-22-repo", "c263d6808e04d509d1a48b17fa6016733bb1fce6"),
        ("ducktape/2025-11-22-repo-2", "a81550a1af11e03e397ff8344b1aa818658113db"),
    ]

    print("=== Building specimen bundle ===")
    print()

    # Create temporary bundle repository
    with tempfile.TemporaryDirectory(prefix="specimens-bundle-") as tmpdir:
        bundle_repo_path = Path(tmpdir) / "bundle"
        bundle_repo_path.mkdir()

        # Initialize bundle repo
        bundle_repo = pygit2.init_repository(str(bundle_repo_path))

        # Create base commit
        sig = pygit2.Signature("Bundle Builder", "bundle@example.com")
        tree_oid = bundle_repo.TreeBuilder().write()
        base_commit_oid = bundle_repo.create_commit("refs/heads/main", sig, sig, "Bundle base commit", tree_oid, [])
        base_commit = bundle_repo[base_commit_oid]
        print(f"Base commit: {base_commit_oid}")
        print()

        # Process each specimen
        for spec_id, source_commit_sha in specimen_configs:
            # Read manifest
            manifest_path = specimens_dir / spec_id / "manifest.yaml"
            if not manifest_path.exists():
                print(f"Warning: No manifest for {spec_id}, skipping")
                continue

            with manifest_path.open() as f:
                manifest_data = yaml.safe_load(f)

            # Parse into SpecimenDoc
            specimen = TypeAdapter(SpecimenDoc).validate_python(manifest_data)

            # Extract bundle filters
            include = specimen.bundle.include if specimen.bundle else None
            exclude = specimen.bundle.exclude if specimen.bundle else None

            # Derive tag name from ref in manifest
            ref = specimen.source.ref
            tag_name = ref.removeprefix("refs/tags/") if ref.startswith("refs/tags/") else f"specimen-{spec_id}"

            # Create filtered commit
            create_filtered_commit(
                source_repo=source_repo,
                bundle_repo=bundle_repo,
                source_commit_sha=source_commit_sha,
                tag_name=tag_name,
                base_commit=base_commit,
                include=include,
                exclude=exclude,
            )

        # Create bundle using git subprocess - libgit2/pygit2 doesn't support bundle creation
        # See: https://github.com/libgit2/pygit2/issues/722
        subprocess.run(["git", "bundle", "create", str(output_bundle), "--all"], cwd=bundle_repo_path, check=True)

        # Show result
        size_mb = output_bundle.stat().st_size / 1024 / 1024
        print(f"=== Bundle created: {output_bundle} ({size_mb:.1f}MB) ===")

        # Verify bundle
        print()
        print("=== Verifying bundle ===")
        verify_dir = Path(tmpdir) / "verify"
        verify_repo = pygit2.clone_repository(str(output_bundle), str(verify_dir))
        tags = sorted(verify_repo.listall_references())
        tag_names = [t.removeprefix("refs/tags/") for t in tags if t.startswith("refs/tags/")]
        print(f"Tags in bundle: {', '.join(tag_names)}")

        if tag_names:
            verify_repo.set_head(f"refs/tags/{tag_names[0]}")
            verify_repo.checkout_head()
            print(f"✓ Successfully checked out {tag_names[0]}")


if __name__ == "__main__":
    main()
