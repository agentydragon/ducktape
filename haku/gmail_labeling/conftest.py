"""Shared test fakes/fixtures for gmail_labeling."""

import pytest

from gmail_api.labels import GmailLabel, LabelType
from haku.gmail_labeling.client import LabelClient
from haku.gmail_labeling.namespace import LabelNamespace


class FakeLabelBackend:
    """In-memory `LabelBackend` that records thread modifications.

    No namespace enforcement (that lives in `LabelClient`), so tests can assert
    the client refuses out-of-namespace names before any backend call happens.
    """

    def __init__(self, labels: list[GmailLabel] | None = None) -> None:
        self._labels: dict[str, GmailLabel] = {label.id: label for label in (labels or [])}
        self._created = 0
        self.thread_mods: list[tuple[str, list[str], list[str]]] = []

    def list_labels(self) -> list[GmailLabel]:
        return list(self._labels.values())

    def create_label(self, name: str) -> GmailLabel:
        self._created += 1
        label = GmailLabel(id=f"Label_{self._created}", name=name, type=LabelType.USER)
        self._labels[label.id] = label
        return label

    def rename_label(self, label_id: str, new_name: str) -> GmailLabel:
        renamed = GmailLabel(id=label_id, name=new_name, type=LabelType.USER)
        self._labels[label_id] = renamed
        return renamed

    def delete_label(self, label_id: str) -> None:
        del self._labels[label_id]

    def modify_thread(self, thread_id: str, *, add: list[str], remove: list[str]) -> None:
        self.thread_mods.append((thread_id, add, remove))


def user_label(name: str, label_id: str) -> GmailLabel:
    return GmailLabel(id=label_id, name=name, type=LabelType.USER)


@pytest.fixture
def make_client():
    """Factory: (labels, prefix) -> (LabelClient, FakeLabelBackend)."""

    def _make(labels: list[GmailLabel] | None = None, prefix: str = "haku/") -> tuple[LabelClient, FakeLabelBackend]:
        backend = FakeLabelBackend(labels)
        return LabelClient(backend, LabelNamespace(prefix)), backend

    return _make


@pytest.fixture
def backend() -> FakeLabelBackend:
    """A fresh empty backend (request alongside `client` to assert on its calls)."""
    return FakeLabelBackend()


@pytest.fixture
def client(backend: FakeLabelBackend) -> LabelClient:
    """A `LabelClient` over `backend` with the default `haku/` namespace."""
    return LabelClient(backend, LabelNamespace("haku/"))
