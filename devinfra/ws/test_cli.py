from datetime import UTC, datetime

import pytest
import typer
from typer.testing import CliRunner

from devinfra.ws.cli import app, claim_manifest, parse_ttl, shutdown_time

NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(("ttl", "seconds"), [("90m", 5400), ("8h", 28800), ("3d", 259200)])
def test_parse_ttl(ttl: str, seconds: int) -> None:
    assert parse_ttl(ttl).total_seconds() == seconds


@pytest.mark.parametrize("bad", ["", "8", "h8", "8w", "-1h", "8 h"])
def test_parse_ttl_rejects(bad: str) -> None:
    with pytest.raises(typer.BadParameter):
        parse_ttl(bad)


def test_shutdown_time_rfc3339() -> None:
    assert shutdown_time("8h", NOW) == "2026-07-16T20:00:00Z"


def test_claim_manifest_shape() -> None:
    m = claim_manifest("ws-test", "90m", NOW, "zai")
    assert m["metadata"] == {"name": "ws-test", "namespace": "agent-workspaces"}
    assert m["spec"]["warmPoolRef"] == {"name": "zai"}
    assert m["spec"]["lifecycle"] == {"shutdownPolicy": "Delete", "shutdownTime": "2026-07-16T13:30:00Z"}


@pytest.mark.parametrize("argv", [[], ["--help"]], ids=["bare", "help"])
def test_quick_reference_rendered(argv: list[str]) -> None:
    output = CliRunner().invoke(app, argv).output
    # Each example survives as its own line — catches rich/click re-wrapping
    # collapsing the block, not just the substrings existing somewhere.
    for example in ["ws new -t codex exp --ttl 3d", "ws extend exp 24h", "ws rm exp / ws rm --all"]:
        assert any(example in line for line in output.splitlines()), f"{example}\n----\n{output}"


def test_quick_reference_alignment_survives() -> None:
    # The two columns stay apart — proves the fenced block rendered verbatim
    # rather than re-flowed into prose.
    output = CliRunner().invoke(app, ["--help"]).output
    line = next(ln for ln in output.splitlines() if "ws templates" in ln)
    assert "ws templates                   LLM lanes" in line


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
