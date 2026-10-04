import textwrap
import tomllib

import pytest_bazel

from nix.home.codex import merge


def test_main_prints_unmanaged_toml_and_preserves_live_only_config(tmp_path, monkeypatch, capsys) -> None:
    base = tmp_path / "config.nix-base.toml"
    live = tmp_path / "config.toml"
    base.write_text(
        textwrap.dedent(
            """
            approval_policy = "on-request"

            [features]
            streamable_shell = true

            [profiles.openai]
            model = "gpt-5.1-codex"
            """
        )
    )
    live.write_text(
        textwrap.dedent(
            """
            approval_policy = "never"

            [features]
            streamable_shell = false
            manual_feature = true

            [profiles.openai]
            model = "gpt-5-codex"
            local_note = "keep"

            [profiles.scratch]
            model = "local"

            [projects."/repo"]
            trust_level = "trusted"
            """
        )
    )
    monkeypatch.setenv("BASE", str(base))
    monkeypatch.setenv("LIVE", str(live))

    merge.main()

    captured = capsys.readouterr()
    assert captured.err == textwrap.dedent(
        """\
        codex config merge: preserved unmanaged live config TOML:
        [features]
        manual_feature = true

        [profiles.openai]
        local_note = "keep"

        [profiles.scratch]
        model = "local"

        [projects."/repo"]
        trust_level = "trusted"
        """
    )

    result = tomllib.loads(live.read_text())
    assert result["approval_policy"] == "on-request"
    assert result["features"]["streamable_shell"] is True
    assert result["features"]["manual_feature"] is True
    assert result["profiles"]["openai"]["model"] == "gpt-5.1-codex"
    assert result["profiles"]["openai"]["local_note"] == "keep"
    assert result["profiles"]["scratch"]["model"] == "local"
    assert result["projects"]["/repo"]["trust_level"] == "trusted"


def test_main_adds_github_pr_app_tool_approvals(tmp_path, monkeypatch) -> None:
    base = tmp_path / "config.nix-base.toml"
    live = tmp_path / "config.toml"
    base.write_text(
        textwrap.dedent(
            """
            [apps.connector_76869538009648d5b282a4bb21c3d157.tools.github_create_pull_request]
            approval_mode = "approve"

            [apps.connector_76869538009648d5b282a4bb21c3d157.tools.github_update_pull_request]
            approval_mode = "approve"

            [apps.connector_76869538009648d5b282a4bb21c3d157.tools.create_pull_request]
            approval_mode = "approve"

            [apps.connector_76869538009648d5b282a4bb21c3d157.tools.update_pull_request]
            approval_mode = "approve"
            """
        )
    )
    live.write_text(
        textwrap.dedent(
            """
            model = "local"

            [apps.slack]
            enabled = true
            """
        )
    )
    monkeypatch.setenv("BASE", str(base))
    monkeypatch.setenv("LIVE", str(live))

    merge.main()

    result = tomllib.loads(live.read_text())
    assert result["model"] == "local"
    assert result["apps"]["slack"]["enabled"] is True
    github_connector = result["apps"]["connector_76869538009648d5b282a4bb21c3d157"]
    assert github_connector["tools"]["github_create_pull_request"]["approval_mode"] == "approve"
    assert github_connector["tools"]["github_update_pull_request"]["approval_mode"] == "approve"
    assert github_connector["tools"]["create_pull_request"]["approval_mode"] == "approve"
    assert github_connector["tools"]["update_pull_request"]["approval_mode"] == "approve"


if __name__ == "__main__":
    pytest_bazel.main()
