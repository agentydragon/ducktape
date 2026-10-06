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


def test_deep_merge_adds_base_only_table_beside_live_entries() -> None:
    live = {"live_scalar": "live-value", "shared": {"live_only": {"key": "live-value"}}}
    base = {"shared": {"base_only": {"nested": {"key": "base-value"}}}}

    assert merge.deep_merge(live, base) == {
        "live_scalar": "live-value",
        "shared": {"live_only": {"key": "live-value"}, "base_only": {"nested": {"key": "base-value"}}},
    }


if __name__ == "__main__":
    pytest_bazel.main()
