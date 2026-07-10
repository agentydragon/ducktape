from pathlib import Path

import pytest_bazel

from devinfra import prettier_cli


def test_prettier_format_yaml_in_place_ignores_repo_config(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(prettier_cli.subprocess, "run", fake_run)

    prettier_cli.prettier_format_yaml_in_place(Path("secrets/haku-attic.yaml"))

    assert calls == [
        (
            [
                "prettier",
                "--write",
                "--no-config",
                "--parser",
                "yaml",
                "--print-width",
                "120",
                "--tab-width",
                "2",
                "--no-use-tabs",
                "secrets/haku-attic.yaml",
            ],
            {"check": True},
        )
    ]


if __name__ == "__main__":
    pytest_bazel.main()
