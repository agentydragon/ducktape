import json
import os
import sys
from pathlib import Path

import pytest
import pytest_bazel

import tools.claude_hooks.config.podman.crun_gvisor_wrapper as wrapper
from tools.claude_hooks.config.podman.crun_gvisor_wrapper import (
    ANNOTATION_KEY,
    ANNOTATION_VALUE,
    REAL_CRUN,
    CrunArgs,
    inject_annotation,
    inject_no_new_keyring,
    main,
    parse_crun_args,
)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        pytest.param(
            [],
            CrunArgs(command=None, command_idx=None, bundle_dir=None, has_no_new_keyring=False, container_id=None),
            id="empty",
        ),
        pytest.param(
            ["--version"],
            CrunArgs(command=None, command_idx=None, bundle_dir=None, has_no_new_keyring=False, container_id=None),
            id="no-subcommand",
        ),
        pytest.param(
            ["create", "-b", "/b", "ctr1"],
            CrunArgs(
                command="create", command_idx=0, bundle_dir=Path("/b"), has_no_new_keyring=False, container_id=None
            ),
            id="create-bundle-short",
        ),
        pytest.param(
            ["create", "--bundle", "/b"],
            CrunArgs(
                command="create", command_idx=0, bundle_dir=Path("/b"), has_no_new_keyring=False, container_id=None
            ),
            id="create-bundle-long",
        ),
        pytest.param(
            ["create", "--bundle=/b"],
            CrunArgs(
                command="create", command_idx=0, bundle_dir=Path("/b"), has_no_new_keyring=False, container_id=None
            ),
            id="create-bundle-equals",
        ),
        pytest.param(
            ["create", "--no-new-keyring", "-b", "/b"],
            CrunArgs(
                command="create", command_idx=0, bundle_dir=Path("/b"), has_no_new_keyring=True, container_id=None
            ),
            id="create-no-new-keyring",
        ),
        pytest.param(
            ["create", "ctr1"],
            CrunArgs(command="create", command_idx=0, bundle_dir=None, has_no_new_keyring=False, container_id=None),
            id="create-no-bundle",
        ),
        pytest.param(
            ["run", "-b", "/b", "ctr1"],
            CrunArgs(command="run", command_idx=0, bundle_dir=Path("/b"), has_no_new_keyring=False, container_id=None),
            id="run-bundle",
        ),
        pytest.param(
            ["exec", "ctr123", "/bin/sh"],
            CrunArgs(command="exec", command_idx=0, bundle_dir=None, has_no_new_keyring=False, container_id="ctr123"),
            id="exec-simple",
        ),
        pytest.param(
            ["exec", "--cwd", "/tmp", "-u", "root", "-e", "FOO=bar", "ctr123", "/bin/sh"],
            CrunArgs(command="exec", command_idx=0, bundle_dir=None, has_no_new_keyring=False, container_id="ctr123"),
            id="exec-with-flags",
        ),
        pytest.param(
            [
                "exec",
                "--console-socket",
                "/tmp/sock",
                "--pid-file",
                "/tmp/pid",
                "--preserve-fds",
                "3",
                "-p",
                "/tmp/process.json",
                "--apparmor",
                "profile",
                "--cap",
                "CAP_NET_ADMIN",
                "myctr",
                "/bin/bash",
            ],
            CrunArgs(command="exec", command_idx=0, bundle_dir=None, has_no_new_keyring=False, container_id="myctr"),
            id="exec-all-value-flags",
        ),
        pytest.param(
            ["exec"],
            CrunArgs(command="exec", command_idx=0, bundle_dir=None, has_no_new_keyring=False, container_id=None),
            id="exec-no-container-id",
        ),
        pytest.param(
            ["--debug", "create", "-b", "/b"],
            CrunArgs(
                command="create", command_idx=1, bundle_dir=Path("/b"), has_no_new_keyring=False, container_id=None
            ),
            id="global-flags-before-subcommand",
        ),
        pytest.param(
            ["delete", "ctr1"],
            CrunArgs(command="delete", command_idx=0, bundle_dir=None, has_no_new_keyring=False, container_id=None),
            id="other-subcommand",
        ),
        pytest.param(
            ["--root", "/tmp", "run", "-b", "/x"],
            CrunArgs(command="run", command_idx=2, bundle_dir=Path("/x"), has_no_new_keyring=False, container_id=None),
            id="command-idx-after-global-flags",
        ),
    ],
)
def test_parse_crun_args(args: list[str], expected: CrunArgs) -> None:
    assert parse_crun_args(args) == expected


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        pytest.param(["create", "-b", "/b"], ["create", "--no-new-keyring", "-b", "/b"], id="injects-for-create"),
        pytest.param(["run", "-b", "/b"], ["run", "--no-new-keyring", "-b", "/b"], id="injects-for-run"),
        pytest.param(
            ["create", "--no-new-keyring", "-b", "/b"], ["create", "--no-new-keyring", "-b", "/b"], id="no-duplicate"
        ),
        pytest.param(["exec", "ctr1", "/bin/sh"], ["exec", "ctr1", "/bin/sh"], id="skips-exec"),
        pytest.param(["delete", "ctr1"], ["delete", "ctr1"], id="skips-other-subcommand"),
        pytest.param(
            ["--debug", "create", "-b", "/b"],
            ["--debug", "create", "--no-new-keyring", "-b", "/b"],
            id="preserves-global-flags",
        ),
    ],
)
def test_inject_no_new_keyring(args: list[str], expected: list[str]) -> None:
    parsed = parse_crun_args(args)
    assert inject_no_new_keyring(args, parsed) == expected


class TestInjectAnnotation:
    def test_injects_annotation(self, tmp_path: Path) -> None:
        config: dict[str, object] = {"process": {}}
        (tmp_path / "config.json").write_text(json.dumps(config))
        inject_annotation(tmp_path)
        result = json.loads((tmp_path / "config.json").read_text())
        assert result["annotations"][ANNOTATION_KEY] == ANNOTATION_VALUE

    def test_preserves_existing_annotations(self, tmp_path: Path) -> None:
        config = {"annotations": {"other": "value"}}
        (tmp_path / "config.json").write_text(json.dumps(config))
        inject_annotation(tmp_path)
        result = json.loads((tmp_path / "config.json").read_text())
        assert result["annotations"]["other"] == "value"
        assert result["annotations"][ANNOTATION_KEY] == ANNOTATION_VALUE

    def test_skips_if_already_set(self, tmp_path: Path) -> None:
        config = {"annotations": {ANNOTATION_KEY: ANNOTATION_VALUE}}
        original = json.dumps(config)
        (tmp_path / "config.json").write_text(original)
        inject_annotation(tmp_path)
        assert (tmp_path / "config.json").read_text() == original

    def test_skips_if_no_config(self, tmp_path: Path) -> None:
        inject_annotation(tmp_path)


ExecvCall = tuple[str, list[str]]


@pytest.fixture
def execv_capture(monkeypatch: pytest.MonkeyPatch) -> list[ExecvCall]:
    """Intercept os.execv and capture (path, argv) calls."""
    calls: list[ExecvCall] = []
    monkeypatch.setattr(os, "execv", lambda path, argv: calls.append((path, argv)))
    return calls


class TestMain:
    """Integration tests for main() with os.execv intercepted."""

    def test_create_injects_keyring_and_annotation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, execv_capture: list[ExecvCall]
    ) -> None:
        config: dict[str, object] = {"process": {}}
        (tmp_path / "config.json").write_text(json.dumps(config))
        monkeypatch.setattr(sys, "argv", ["crun", "create", "-b", str(tmp_path), "ctr1"])

        main()

        assert len(execv_capture) == 1
        path, exec_argv = execv_capture[0]
        assert path == REAL_CRUN
        assert exec_argv[0] == REAL_CRUN
        assert "--no-new-keyring" in exec_argv
        result = json.loads((tmp_path / "config.json").read_text())
        assert result["annotations"][ANNOTATION_KEY] == ANNOTATION_VALUE

    def test_create_already_has_keyring_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, execv_capture: list[ExecvCall]
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["crun", "create", "--no-new-keyring", "-b", str(tmp_path)])

        main()

        _, exec_argv = execv_capture[0]
        assert exec_argv.count("--no-new-keyring") == 1

    def test_exec_creates_mock_freezer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, execv_capture: list[ExecvCall]
    ) -> None:
        freezer_base = tmp_path / "freezer"
        monkeypatch.setattr(wrapper, "FREEZER_BASE", freezer_base)
        monkeypatch.setattr(sys, "argv", ["crun", "exec", "abc123", "/bin/sh"])

        main()

        _, exec_argv = execv_capture[0]
        assert "--no-new-keyring" not in exec_argv
        freezer_state = freezer_base / "libpod_parent" / "libpod-abc123" / "freezer.state"
        assert freezer_state.read_text() == "THAWED"

    def test_passthrough_no_subcommand(self, monkeypatch: pytest.MonkeyPatch, execv_capture: list[ExecvCall]) -> None:
        monkeypatch.setattr(sys, "argv", ["crun", "--version"])

        main()

        _, exec_argv = execv_capture[0]
        assert exec_argv == [REAL_CRUN, "--version"]

    def test_global_flags_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, execv_capture: list[ExecvCall]
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["crun", "--debug", "create", "-b", str(tmp_path)])

        main()

        _, exec_argv = execv_capture[0]
        assert exec_argv[1] == "--debug"
        assert exec_argv[2] == "create"
        assert exec_argv[3] == "--no-new-keyring"


if __name__ == "__main__":
    pytest_bazel.main()
