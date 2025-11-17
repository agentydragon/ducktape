from pathlib import Path

import pygit2

from adgn.mcp.git_ro.server import GIT_RO_SERVER_NAME, DiffFormat, DiffInput, ListSlice, make_git_ro_server


async def test_git_ro_stat_counts(tmp_path: Path, make_typed_mcp) -> None:
    """Create a repo, make a staged change, call git_diff(format=stat) and assert additions/deletions."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    repo = pygit2.init_repository(str(repo_dir), initial_head="main")

    # initial commit
    (repo_dir / "file.txt").write_text("line1\n")
    repo.index.add("file.txt")
    repo.index.write()
    sig = pygit2.Signature("Test", "test@example.com")
    tree_oid = repo.index.write_tree()
    repo.create_commit("HEAD", sig, sig, "initial", tree_oid, [])

    # modify and stage a non-trivial diff (add two lines)
    (repo_dir / "file.txt").write_text("line1\nline2\nline3\n")
    repo.index.add("file.txt")
    repo.index.write()

    server = make_git_ro_server(repo_dir)

    async with make_typed_mcp(server, GIT_RO_SERVER_NAME) as (client, session):
        # Call the git_diff tool with format=stat and staged=True
        result = await client.git_diff(
            DiffInput(format=DiffFormat.STAT, staged=True, find_renames=True, list_slice=ListSlice(offset=0, limit=100))
        )

        # result is a flattened StatResult (DiffStatPage fields directly available)
        items = result.items
        assert isinstance(items, list)

        # Find the file.txt item and compare all fields at once
        file_items = [it for it in items if it.path == "file.txt"]
        assert len(file_items) == 1, "Expected exactly one file.txt in stat items"
        file_item = file_items[0]
        assert (int(file_item.additions), int(file_item.deletions)) == (2, 0)
