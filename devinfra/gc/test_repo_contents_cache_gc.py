import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.gc import output_base_gc, repo_contents_cache_gc as gc
from devinfra.gc.conftest import link_external, make_base, make_cache_entry


def _hash(char: str) -> str:
    return char * 64


def _uuid(char: str) -> str:
    return f"{char * 8}-{char * 4}-{char * 4}-{char * 4}-{char * 12}"


@dataclass(frozen=True, slots=True)
class FakeCache:
    root: Path  # the output user root
    contents: Path
    kept_base: Path  # its workspace exists
    prunable_base: Path  # its workspace is gone
    referenced: Path  # linked from both bases
    prunable_only: Path  # linked only from the prunable base
    unreferenced: Path
    missing_inputs: Path  # a directory without its recorded-inputs file
    malformed: Path  # a directory that is not a UUID


@pytest.fixture
def cache(tmp_path: Path) -> FakeCache:
    root = tmp_path / "output"
    contents = gc.repo_contents_cache(root)
    contents.mkdir(parents=True)
    (contents / "_trash").mkdir()
    (contents / "gc_lock").touch()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kept_base = make_base(root, workspace)
    prunable_base = make_base(root, tmp_path / "gone")

    referenced = make_cache_entry(contents, _hash("a"), _uuid("1"))
    prunable_only = make_cache_entry(contents, _hash("b"), _uuid("2"))
    unreferenced = make_cache_entry(contents, _hash("c"), _uuid("3"))
    missing_inputs = make_cache_entry(contents, _hash("d"), _uuid("4"), recorded_inputs=False)
    malformed = contents / _hash("e") / "not-a-uuid"
    malformed.mkdir(parents=True)

    link_external(kept_base, "referenced_repo", referenced)
    link_external(kept_base, "gone_repo", contents / _hash("f") / _uuid("6"))  # dangling: the entry is gone
    link_external(kept_base, "_main", workspace)  # a link outside the cache
    (kept_base / "external" / "@referenced_repo.marker").write_text("")  # a regular file
    link_external(prunable_base, "referenced_repo", referenced)
    link_external(prunable_base, "prunable_only_repo", prunable_only)
    return FakeCache(
        root, contents, kept_base, prunable_base, referenced, prunable_only, unreferenced, missing_inputs, malformed
    )


def _bases(cache: FakeCache, proc: Path, mountinfo: Path) -> list[output_base_gc.Inspection]:
    return output_base_gc.scan_output_user_root(cache.root, session_bases=(), proc_root=proc, mountinfo_path=mountinfo)


def _scan(cache: FakeCache, proc: Path, mountinfo: Path) -> dict[Path, gc.EntryInspection]:
    entries = gc.scan_repo_contents_cache(cache.contents, _bases(cache, proc, mountinfo), mountinfo_path=mountinfo)
    return {gc.entry_path(item): item for item in entries}


def _prunable(cache: FakeCache, proc: Path, mountinfo: Path) -> list[gc.PrunableEntry]:
    return [item for item in _scan(cache, proc, mountinfo).values() if isinstance(item, gc.PrunableEntry)]


def _delete(cache: FakeCache, candidates: list[gc.PrunableEntry], mountinfo: Path) -> list[gc.EntryDeletionResult]:
    return gc.delete_prunable_entries(
        candidates, contents=cache.contents, output_user_root=cache.root, session_bases=(), mountinfo_path=mountinfo
    )


def test_entries_are_classified_by_who_links_to_them(cache: FakeCache, proc: Path, mountinfo: Path) -> None:
    by_path = _scan(cache, proc, mountinfo)

    assert isinstance(by_path[cache.unreferenced], gc.PrunableEntry)
    referenced = by_path[cache.referenced]
    assert isinstance(referenced, gc.RetainedEntry)
    assert referenced.reason is gc.RetainedEntryReason.REFERENCED
    assert referenced.referrers == {cache.kept_base, cache.prunable_base}
    prunable_only = by_path[cache.prunable_only]
    assert isinstance(prunable_only, gc.RetainedEntry)
    assert prunable_only.reason is gc.RetainedEntryReason.ONLY_PRUNABLE_REFERRERS
    assert prunable_only.referrers == {cache.prunable_base}
    missing_inputs = by_path[cache.missing_inputs]
    assert isinstance(missing_inputs, gc.ReviewEntry)
    assert missing_inputs.reason is gc.ReviewEntryReason.MISSING_RECORDED_INPUTS
    malformed = by_path[cache.malformed]
    assert isinstance(malformed, gc.ReviewEntry)
    assert malformed.reason is gc.ReviewEntryReason.MALFORMED_NAME
    # The dangling link names no entry, `_trash/` and `gc_lock` are structure: five rows in all.
    assert len(by_path) == 5


def test_a_link_through_a_symlinked_cache_path_is_a_reference(
    cache: FakeCache, proc: Path, mountinfo: Path, tmp_path: Path
) -> None:
    # Bazel writes the link via `--repo_contents_cache` as configured, e.g. through a symlinked
    # home; the scan resolves the cache, so the two spellings must still match.
    alias = tmp_path / "alias-contents"
    alias.symlink_to(cache.contents)
    link_external(cache.kept_base, "aliased_repo", alias / cache.unreferenced.relative_to(cache.contents))

    item = _scan(cache, proc, mountinfo)[cache.unreferenced]

    assert isinstance(item, gc.RetainedEntry)
    assert item.referrers == {cache.kept_base}


def test_recorded_inputs_without_a_directory_requires_review(cache: FakeCache, proc: Path, mountinfo: Path) -> None:
    orphan = cache.contents / _hash("a") / (_uuid("9") + gc.RECORDED_INPUTS_SUFFIX)
    orphan.write_text(_hash("a"))

    item = _scan(cache, proc, mountinfo)[orphan]

    assert isinstance(item, gc.ReviewEntry)
    assert item.reason is gc.ReviewEntryReason.MISSING_CONTENTS


def test_entries_only_deleted_bases_linked_to_become_prunable(cache: FakeCache, proc: Path, mountinfo: Path) -> None:
    """The `all --prune` coupling: bases go first, then a re-scan frees their entries."""
    bases = [item for item in _bases(cache, proc, mountinfo) if isinstance(item, output_base_gc.PrunableBase)]
    assert [item.path for item in bases] == [cache.prunable_base]
    output_base_gc.delete_prunable_bases(bases, proc_root=proc, mountinfo_path=mountinfo)

    by_path = _scan(cache, proc, mountinfo)

    assert isinstance(by_path[cache.prunable_only], gc.PrunableEntry)
    assert isinstance(by_path[cache.referenced], gc.RetainedEntry)  # the kept base still links to it


def test_delete_removes_exactly_the_unreferenced_entries(cache: FakeCache, proc: Path, mountinfo: Path) -> None:
    results = _delete(cache, _prunable(cache, proc, mountinfo), mountinfo)

    assert results == [gc.DeletedEntry(cache.unreferenced)]
    assert not cache.unreferenced.exists()
    assert not gc.recorded_inputs_of(cache.unreferenced).exists()
    for survivor in (cache.referenced, cache.prunable_only):
        assert survivor.is_dir()
        assert gc.recorded_inputs_of(survivor).is_file()
    assert cache.missing_inputs.is_dir()
    assert cache.malformed.is_dir()
    assert list((cache.contents / "_trash").iterdir()) == []


def test_delete_skips_an_entry_a_base_linked_to_since_the_scan(cache: FakeCache, proc: Path, mountinfo: Path) -> None:
    candidates = _prunable(cache, proc, mountinfo)
    link_external(cache.kept_base, "late_repo", cache.unreferenced)

    [result] = _delete(cache, candidates, mountinfo)

    assert isinstance(result, gc.SkippedEntry)
    assert cache.unreferenced.is_dir()


def test_delete_sees_a_base_created_since_the_scan(
    cache: FakeCache, proc: Path, mountinfo: Path, tmp_path: Path
) -> None:
    candidates = _prunable(cache, proc, mountinfo)
    new_workspace = tmp_path / "new-workspace"
    new_workspace.mkdir()
    link_external(make_base(cache.root, new_workspace), "late_repo", cache.unreferenced)

    [result] = _delete(cache, candidates, mountinfo)

    assert isinstance(result, gc.SkippedEntry)
    assert cache.unreferenced.is_dir()


def test_delete_skips_a_replaced_entry(cache: FakeCache, proc: Path, mountinfo: Path) -> None:
    candidates = _prunable(cache, proc, mountinfo)
    replacement = cache.contents / "replacement"
    cache.unreferenced.rename(replacement)
    cache.unreferenced.mkdir()

    [result] = _delete(cache, candidates, mountinfo)

    assert isinstance(result, gc.SkippedEntry)
    assert cache.unreferenced.is_dir()


def test_delete_yields_to_a_running_bazel_command(cache: FakeCache, proc: Path, mountinfo: Path) -> None:
    """Bazel holds `gc_lock` shared for a command's whole duration; nothing is removed then."""
    candidates = _prunable(cache, proc, mountinfo)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_RDWR); "
            "fcntl.lockf(fd, fcntl.LOCK_SH); print('locked', flush=True); sys.stdin.read(1)",
            str(cache.contents / "gc_lock"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline() == "locked\n"
        results = _delete(cache, candidates, mountinfo)
    finally:
        assert holder.stdin is not None
        holder.communicate("x", timeout=5)

    assert [type(result) for result in results] == [gc.SkippedEntry]
    assert cache.unreferenced.is_dir()
    assert gc.recorded_inputs_of(cache.unreferenced).is_file()


def test_report_names_entries_relative_to_the_cache(cache: FakeCache, proc: Path, mountinfo: Path) -> None:
    entries = list(_scan(cache, proc, mountinfo).values())

    report = gc.render_report(entries, contents=cache.contents, include_kept=False, include_sizes=False)

    rows = [line.split() for line in report.splitlines()[1:-1]]
    assert sorted(row[0] for row in rows) == ["PRUNE", "REVIEW", "REVIEW"]
    assert f"{_hash('c')}/{_uuid('3')}" in {row[2] for row in rows}


@pytest.mark.parametrize("reason", [*gc.RetainedEntryReason, *gc.ReviewEntryReason], ids=str)
def test_every_reason_is_described(reason: gc.RetainedEntryReason | gc.ReviewEntryReason) -> None:
    entry = gc.CacheEntry(Path("/test-contents/hash/uuid"), output_base_gc.FileIdentity(0, 0), 0)
    inspection: gc.RetainedEntry | gc.ReviewEntry
    if isinstance(reason, gc.RetainedEntryReason):
        inspection = gc.RetainedEntry(entry, reason, frozenset({Path("/test-base")}))
    else:
        inspection = gc.ReviewEntry(entry.contents, reason, detail="test-detail")
    assert gc.describe_reason(inspection)


if __name__ == "__main__":
    pytest_bazel.main()
