import pytest
import pytest_bazel
from fastmcp.exceptions import ToolError

from gmail_api.labels import GmailLabel, LabelType
from haku.gmail_labeling.conftest import user_label


def test_apply_creates_label_and_records_modify(make_client):
    client, backend = make_client()
    label = client.apply_label("t1", "haku/triaged")
    assert label.name == "haku/triaged"
    assert backend.thread_mods == [("t1", [label.id], [])]


def test_apply_reuses_existing_label(make_client):
    client, backend = make_client([user_label("haku/triaged", "Label_7")])
    label = client.apply_label("t1", "haku/triaged")
    assert label.id == "Label_7"
    # No new label created.
    assert {label.id for label in backend.list_labels()} == {"Label_7"}


def test_apply_rejects_outside_prefix_before_any_io(make_client):
    client, backend = make_client()
    with pytest.raises(ToolError):
        client.apply_label("t1", "Important")
    # Enforcement happens before touching the backend.
    assert backend.thread_mods == []
    assert backend.list_labels() == []


def test_apply_rejects_system_label(make_client):
    client, backend = make_client()
    with pytest.raises(ToolError):
        client.apply_label("t1", "INBOX")
    assert backend.thread_mods == []


def test_remove_requires_existing_label(make_client):
    client, _ = make_client()
    with pytest.raises(ToolError):
        client.remove_label("t1", "haku/nope")


def test_remove_records_modify(make_client):
    client, backend = make_client([user_label("haku/triaged", "Label_3")])
    client.remove_label("t1", "haku/triaged")
    assert backend.thread_mods == [("t1", [], ["Label_3"])]


def test_create_duplicate_rejected(make_client):
    client, _ = make_client([user_label("haku/x", "Label_1")])
    with pytest.raises(ToolError):
        client.create_label("haku/x")


def test_rename_within_namespace(make_client):
    client, _ = make_client([user_label("haku/a", "Label_1")])
    renamed = client.rename_label("haku/a", "haku/b")
    assert renamed.name == "haku/b"
    assert renamed.id == "Label_1"


def test_rename_rejects_outside_destination(make_client):
    client, _ = make_client([user_label("haku/a", "Label_1")])
    with pytest.raises(ToolError):
        client.rename_label("haku/a", "Inbox-Critical")


def test_rename_rejects_outside_source(make_client):
    client, _ = make_client([user_label("Promotions", "Label_9")])
    with pytest.raises(ToolError):
        client.rename_label("Promotions", "haku/a")


def test_delete_rejects_outside_prefix(make_client):
    client, _ = make_client()
    with pytest.raises(ToolError):
        client.delete_label("Important")


def test_delete_removes_existing(make_client):
    client, backend = make_client([user_label("haku/x", "Label_1")])
    client.delete_label("haku/x")
    assert backend.list_labels() == []


def test_list_filters_to_prefixed_user_labels(make_client):
    labels = [
        user_label("haku/x", "Label_1"),
        user_label("Other", "Label_2"),
        GmailLabel(id="INBOX", name="INBOX", type=LabelType.SYSTEM),
    ]
    client, _ = make_client(labels)
    listed = client.list_labels()
    assert [label.name for label in listed] == ["haku/x"]


if __name__ == "__main__":
    pytest_bazel.main()
