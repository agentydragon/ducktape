"""Exercise label listing and pruning with a fake Gmail client."""

from unittest.mock import Mock, call

import pytest
import pytest_bazel
from typer.testing import CliRunner

from gmail_api.labels import GmailLabel, LabelType
from gmail_archiver.cli import labels
from gmail_archiver.gmail_api_models import FilterAction, GmailFilter
from gmail_archiver.gmail_client import GmailClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Mock:
    client = Mock(spec=GmailClient)
    client.list_labels_full.return_value = [
        GmailLabel(id="INBOX", name="INBOX", type=LabelType.SYSTEM),
        GmailLabel(id="L_add", name="Added"),
        GmailLabel(id="L_remove", name="Removed"),
        GmailLabel(id="L_unused", name="Unused"),
        GmailLabel(id="L_unused2", name="Unused two"),
    ]
    client.list_filters.return_value = [
        GmailFilter(action=FilterAction(add_label_ids=["L_add"], remove_label_ids=["L_remove"]))
    ]
    monkeypatch.setattr(labels, "get_client", lambda token_file: client)
    return client


@pytest.mark.parametrize("show_system", [False, True])
def test_list_reports_filter_usage(client: Mock, show_system: bool) -> None:
    result = CliRunner().invoke(labels.labels_app, ["list", *(["--system"] if show_system else [])])
    assert result.exit_code == 0, result.output
    assert "4 total, 2 used by filters, 2 unused" in result.output
    assert ("INBOX" in result.output) is show_system
    assert client.mock_calls == [call.list_labels_full(), call.list_filters()]


@pytest.mark.parametrize(
    ("flags", "answer", "execute"),
    [(["--dry-run"], None, False), ([], "n\n", False), (["--no-dry-run"], None, True), ([], "y\n", True)],
)
def test_prune_only_deletes_unreferenced_user_labels(
    client: Mock, flags: list[str], answer: str | None, execute: bool
) -> None:
    client.delete_label.side_effect = [ValueError("Synthetic failure"), None]
    result = CliRunner().invoke(labels.labels_app, ["prune", *flags], input=answer)
    assert result.exit_code == 0, result.output
    assert "Unused labels (2)" in result.output

    expected = [call.list_labels_full(), call.list_filters()]
    if execute:
        expected.extend([call.delete_label("L_unused"), call.delete_label("L_unused2")])
        assert "Synthetic failure" in result.output
        assert "Deleted 1 label(s)" in result.output
    else:
        assert ("DRY RUN" if flags else "Cancelled") in result.output
    assert client.mock_calls == expected


if __name__ == "__main__":
    pytest_bazel.main()
