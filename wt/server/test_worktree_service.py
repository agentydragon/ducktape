"""Integration tests for WorktreeService with real git repositories."""

from pathlib import Path

import pytest
import pytest_bazel

from wt.server.worktree_service import WorktreeService


class TestWorktreeService:
    """Test the WorktreeService with real git repositories."""

    @pytest.fixture
    def service(self, repo_factory, config_factory, service_builder):
        repo_path = repo_factory.create_repo()
        config = config_factory(repo_path).minimal(
            branch_prefix="test/",
            upstream_branch="main",
            github_repo="test-user/test-repo",
            github_enabled=False,
            log_operations=True,
        )

        service = service_builder(config).with_real_git().with_mock_github().build_worktree_service()
        return service, config

    def _make_service(self, repo_factory, config_factory, repo_path=None, *, service_builder, **minimal_kwargs):
        if repo_path is None:
            repo_path = repo_factory.create_repo()
        factory = config_factory(repo_path)
        config = factory.minimal(**minimal_kwargs)

        service = service_builder(config).with_real_git().with_mock_github().build_worktree_service()
        return service, config

    def test_list_worktrees_empty_repo(self, service):
        """Test listing worktrees in empty repository."""
        worktree_service, config = service

        # Fresh repo has no worktrees except main
        result = worktree_service.list_worktrees(config)

        # Should be empty since we filter out the main repo
        assert len(result) == 0

    def test_create_and_list_worktree(self, service):
        """Test creating a worktree and listing it."""
        worktree_service, config = service

        # Create a real worktree
        worktree_path = worktree_service.create_worktree(config, "test-branch")

        # Verify it was created
        assert worktree_path.exists()
        assert worktree_path.name == "test-branch"

        # List worktrees and verify it appears
        result = worktree_service.list_worktrees(config)
        assert len(result) == 1

        name, path, exists = result[0]
        assert name == "test-branch"
        assert path == worktree_path
        assert exists is True

    @pytest.mark.asyncio
    async def test_worktree_removal(self, service):
        """Test removing a worktree."""
        worktree_service, config = service

        # Create a worktree first
        worktree_path = worktree_service.create_worktree(config, "to-remove")
        assert worktree_path.exists()

        # Remove it (using async method)

        await worktree_service.remove_worktree(config, "to-remove", force=True)

        # Verify it's gone
        assert not worktree_path.exists()

        # List should be empty again
        result = worktree_service.list_worktrees(config)
        assert len(result) == 0

    def test_worktree_path_resolution(self, service):
        """Test worktree path methods."""
        worktree_service, config = service

        # Test path calculation
        expected_path = config.worktrees_dir / "test-name"
        actual_path = worktree_service.get_worktree_path(config, "test-name")
        assert actual_path == expected_path

    def test_is_managed_worktree_filtering(self, service):
        """Test worktree filtering logic with real paths."""
        worktree_service, config = service

        # Main repo should not be managed
        main_repo_path = config.main_repo
        assert not worktree_service._is_managed_worktree(main_repo_path, config)

        # Path outside worktrees dir should not be managed
        outside_path = Path("/tmp/outside-worktree")
        assert not worktree_service._is_managed_worktree(outside_path, config)

        # Path inside worktrees dir should be managed
        inside_path = config.worktrees_dir / "valid-worktree"
        assert worktree_service._is_managed_worktree(inside_path, config)

    @pytest.mark.asyncio
    async def test_post_creation_script_execution(self, repo_factory, config_factory, mock_factory, service_builder):
        """Test post-creation script runner executes and passes expected args."""
        repo_path = repo_factory.create_repo()

        script_path = repo_path / "test_script.sh"
        script_path.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
worktree_root=""
for arg in "$@"; do
  case "$arg" in
    --worktree_root=*) worktree_root="${arg#*=}" ;;
  esac
done
if [[ -z "$worktree_root" ]]; then echo "missing --worktree_root" >&2; exit 2; fi
echo "$@" > "$worktree_root/args.txt"
echo "Script executed at: $worktree_root" > "$worktree_root/script_output.txt"
"""
        )
        script_path.chmod(0o755)

        service, config = self._make_service(
            repo_factory,
            config_factory,
            repo_path=repo_path,
            service_builder=service_builder,
            post_creation_script=str(script_path),
        )

        worktree_path = service.create_worktree(config, "script-test")

        result = await WorktreeService.run_post_creation_script(str(script_path), worktree_path)
        assert result["ran"] is True
        assert result["exit_code"] == 0

        output_file = worktree_path / "script_output.txt"
        assert output_file.exists()
        content = output_file.read_text()
        assert str(worktree_path) in content
        args_record = (worktree_path / "args.txt").read_text().strip().split()
        assert f"--worktree_root={worktree_path}" in args_record
        assert f"--worktree_name={worktree_path.name}" in args_record

    def test_copy_hydrates_when_enabled(self, repo_factory, config_factory, service_builder):
        service, config = self._make_service(
            repo_factory, config_factory, service_builder=service_builder, hydrate_worktrees=True
        )
        src = service.create_worktree(config, "src")
        (src / "untracked.txt").write_text("x")
        dst = service.create_worktree(config, "dst", source_worktree=src)
        assert (dst / "untracked.txt").exists()

    def test_copy_skips_hydration_when_disabled(self, repo_factory, config_factory, service_builder):
        service, config = self._make_service(
            repo_factory, config_factory, service_builder=service_builder, hydrate_worktrees=False
        )
        src = service.create_worktree(config, "src")
        (src / "untracked.txt").write_text("x")
        dst = service.create_worktree(config, "dst", source_worktree=src)
        assert not (dst / "untracked.txt").exists()

    def test_create_hydrates_when_enabled(self, repo_factory, config_factory, service_builder):
        service, config = self._make_service(
            repo_factory, config_factory, service_builder=service_builder, hydrate_worktrees=True
        )
        dst = service.create_worktree(config, "dst")
        assert (dst / "README.md").exists()

    def test_create_skips_hydration_when_disabled(self, repo_factory, config_factory, service_builder):
        service, config = self._make_service(
            repo_factory, config_factory, service_builder=service_builder, hydrate_worktrees=False
        )
        dst = service.create_worktree(config, "dst")
        entries = [p for p in dst.iterdir() if p.name != ".git"]
        assert entries == []


if __name__ == "__main__":
    pytest_bazel.main()
