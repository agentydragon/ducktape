"""Exercise the filters commands with a fake Gmail client."""

from pathlib import Path
from unittest.mock import Mock, call

import pytest
from typer.testing import CliRunner

from gmail_archiver.cli import filters
from gmail_archiver.gmail_api_models import CreateFilterRequest, FilterAction, FilterCriteria, GmailFilter
from gmail_archiver.gmail_client import GmailClient


@pytest.mark.parametrize(
    ("command", "mode", "stdin"),
    [
        ("diff", None, None),
        *(
            (command, mode, stdin)
            for command in ("upload", "sync")
            for mode, stdin in (("--dry-run", None), (None, "n\n"), ("--no-dry-run", None), (None, "y\n"))
        ),
    ],
)
def test_filter_commands_preview_or_apply_expected_changes(
    command: str, mode: str | None, stdin: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "filters.yaml"
    source.write_text(
        "- subject: Fails\n  label: Test receipts\n- subject: Works\n  label: Test receipts\n  archive: true\n"
    )
    client = Mock(spec=GmailClient)
    client.list_labels_full.return_value = []
    client.list_filters.return_value = [GmailFilter(id="F_old", criteria=FilterCriteria(subject="Obsolete"))]
    client.get_or_create_label.return_value = "Label_1"
    client.create_filter.side_effect = [ValueError("Synthetic failure"), GmailFilter(id="F_new")]
    monkeypatch.setattr(filters, "get_client", lambda token_file: client)

    result = CliRunner().invoke(filters.filters_app, [command, str(source), *([mode] if mode else [])], input=stdin)

    assert result.exit_code == 0, result.output
    mutations = [request for request in client.mock_calls if request[0] not in {"list_labels_full", "list_filters"}]
    if command == "diff":
        assert "Works" in result.output
        assert "Obsolete" in result.output
        assert not mutations
        return

    if mode == "--dry-run" or stdin == "n\n":
        assert not mutations
        assert ("DRY RUN" if mode else "Cancelled") in result.output
        return

    assert "Synthetic failure" in result.output
    assert mutations == [
        *([call.delete_filter("F_old")] if command == "sync" else []),
        call.get_or_create_label("Test receipts"),
        call.create_filter(
            CreateFilterRequest(
                criteria=FilterCriteria(subject="Fails"), action=FilterAction(add_label_ids=["Label_1"])
            )
        ),
        call.create_filter(
            CreateFilterRequest(
                criteria=FilterCriteria(subject="Works"),
                action=FilterAction(add_label_ids=["Label_1"], remove_label_ids=["INBOX"]),
            )
        ),
    ]
