from __future__ import annotations

from adgn.props.specimens.registry import SpecimenRegistry


async def test_hydrated_copy_only_exposes_scoped_file_for_local_specimen() -> None:
    """Hydrated local specimen workspace should contain only the scoped file.

    For specimen '2025-08-29-pyright_watch_report', the scope includes only
    'pyright_watch_report.py'. The hydrated working directory yielded by the
    context manager must therefore contain exactly one file: that python file.
    """
    rec = SpecimenRegistry.load_strict("2025-08-29-pyright_watch_report")

    async with rec.hydrated_copy() as content_root:
        assert content_root.is_dir(), f"hydrated content root not a directory: {content_root}"
        files = [p.name for p in content_root.iterdir() if p.is_file()]
    assert files == ["pyright_watch_report.py"], (
        f"Expected exactly one file ['pyright_watch_report.py'] in {content_root}, got: {files}"
    )


async def test_hydrated_copy_git_specimen_has_wt_tree_rooted_correctly() -> None:
    """Hydrated git/github specimen should yield a content root whose subtree
    contains wt/wt/server directly under the yielded directory.

    Using specimen '2025-09-02-ducktape_wt', assert that <root>/wt/wt/server exists
    and contains at least one file or subdirectory.
    """
    rec = SpecimenRegistry.load_strict("2025-09-02-ducktape_wt")

    async with rec.hydrated_copy() as root:
        assert root.is_dir(), f"hydrated content root not a directory: {root}"
        server_dir = root / "wt" / "wt" / "server"
        assert server_dir.is_dir(), f"expected directory missing: {server_dir}"
        # There should be some content inside the server directory
        has_any = any(server_dir.iterdir())
        assert has_any, f"expected non-empty directory: {server_dir}"
