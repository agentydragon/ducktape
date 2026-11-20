import asyncio
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from devinfra.gc import output_base_gc, workspace_gc, workspace_run, workspace_scan, worktree_gc
from devinfra.gc.conftest import GitRepo, make_base
from devinfra.gc.removal import RemovalResult


@pytest.mark.parametrize("matching", ["alias", "failed", "ambiguous"])
async def test_base_waits_only_for_unambiguous_resolved_worktree(
    repo: GitRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, matching: str
) -> None:
    checkout = repo.clone("checkout")
    tree = checkout.worktree("tree", "landed")
    other = checkout.worktree("other", "other")
    alias = tmp_path / "alias"
    alias.symlink_to(tree.path, target_is_directory=True)
    root = tmp_path / "output"
    base = make_base(root, tree.path)
    monkeypatch.setattr(output_base_gc, "_TMP_ROOT", tmp_path / "elsewhere")
    resolve = workspace_scan.resolve_path
    calls: Counter[Path] = Counter()

    def resolve_counted(path: Path) -> Path | None:
        calls[path] += 1
        if matching == "failed" and path in {tree.path, alias}:
            return None
        if matching == "ambiguous" and path == other.path:
            return tree.path.resolve()
        return resolve(path)

    monkeypatch.setattr(workspace_scan, "resolve_path", resolve_counted)
    inspected = asyncio.Event()
    inspections = 0
    inspect = workspace_run.Run._inspect_base

    async def inspect_recorded(self: workspace_run.Run, *args: Any, **kwargs: Any) -> None:
        nonlocal inspections
        await inspect(self, *args, **kwargs)
        inspections += 1
        if inspections == 1:
            state = self.bases[base]
            item = state.classification
            assert isinstance(item, output_base_gc.RetainedBase)
            state.classification = replace(item, workspace=alias)
            state.resolved_workspace = resolve_counted(alias)
        inspected.set()

    remove = worktree_gc.remove_worktree

    async def remove_after_inspection(*args: Any, **kwargs: Any) -> RemovalResult[Path]:
        await asyncio.wait_for(inspected.wait(), 10)
        return await remove(*args, **kwargs)

    monkeypatch.setattr(workspace_run.Run, "_inspect_base", inspect_recorded)
    monkeypatch.setattr(worktree_gc, "remove_worktree", remove_after_inspection)
    options = workspace_run.Options(
        domains=frozenset({workspace_run.Domain.WORKTREES, workspace_run.Domain.BASES}),
        root=root,
        session_bases=(),
        workers=4,
        apply=True,
        sizes=False,
        repo=checkout.path,
        pr_source=workspace_gc._without_prs,
    )
    await asyncio.wait_for(workspace_run.Run(options).run(), 30)
    # One worktree resolution plus the first base inspection.
    assert calls[tree.path] == 2
    assert calls[other.path] == 1
    assert inspections == (2 if matching == "alias" else 1)
    assert base.exists() is (matching != "alias")


if __name__ == "__main__":
    pytest_bazel.main()
