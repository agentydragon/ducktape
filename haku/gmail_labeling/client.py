"""Namespace-enforced Gmail label operations.

Every mutating method validates label names against the `LabelNamespace` before
any I/O, so the closure invariant holds regardless of the caller. This is the
component that makes the server safe by construction — enforcement lives here,
in reviewed code, not in the agent's instructions.
"""

from fastmcp.exceptions import ToolError

from gmail_api.labels import GmailLabel, LabelType
from haku.gmail_labeling.backend import LabelBackend
from haku.gmail_labeling.models import Label, ModifyLabelsResult
from haku.gmail_labeling.namespace import LabelNamespace


class LabelClient:
    def __init__(self, backend: LabelBackend, namespace: LabelNamespace) -> None:
        self._backend = backend
        self._ns = namespace

    @property
    def prefix(self) -> str:
        return self._ns.prefix

    def _user_labels(self) -> list[GmailLabel]:
        return [label for label in self._backend.list_labels() if label.type == LabelType.USER]

    def _name_to_id(self) -> dict[str, str]:
        return {label.name: label.id for label in self._user_labels()}

    def list_labels(self) -> list[Label]:
        return [Label(name=label.name, id=label.id) for label in self._user_labels() if self._ns.allows(label.name)]

    def create_label(self, name: str) -> Label:
        self._ns.require(name)
        if name in self._name_to_id():
            raise ToolError(f"label {name!r} already exists")
        created = self._backend.create_label(name)
        return Label(name=created.name, id=created.id)

    def modify_labels(self, thread_ids: list[str], *, add: set[str], remove: set[str]) -> ModifyLabelsResult:
        """Add and/or remove labels across a batch of threads in one call.

        Mirrors Gmail's own `batchModify` shape: one set of IDs, one set of labels to add,
        one set of labels to remove -- applied identically to every thread in `thread_ids`.
        """
        if not thread_ids:
            raise ToolError("thread_ids must be non-empty")
        if not add and not remove:
            raise ToolError("must specify at least one label in `add` or `remove`")
        for name in add | remove:
            self._ns.require(name)
        if overlap := add & remove:
            raise ToolError(f"label(s) {sorted(overlap)} cannot be both added and removed in the same call")

        existing = self._name_to_id()
        if missing := [name for name in remove if name not in existing]:
            raise ToolError(f"label(s) {missing} do not exist")

        added = [self._get_or_create(name, existing) for name in add]
        removed = [Label(name=name, id=existing[name]) for name in remove]
        self._backend.modify_threads(
            thread_ids, add=[label.id for label in added], remove=[label.id for label in removed]
        )
        return ModifyLabelsResult(added=added, removed=removed)

    def _get_or_create(self, name: str, existing: dict[str, str]) -> Label:
        if label_id := existing.get(name):
            return Label(name=name, id=label_id)
        created = self._backend.create_label(name)
        return Label(name=created.name, id=created.id)

    def rename_label(self, old: str, new: str) -> Label:
        self._ns.require(old)
        self._ns.require(new)
        existing = self._name_to_id()
        if old not in existing:
            raise ToolError(f"label {old!r} does not exist")
        if new in existing:
            raise ToolError(f"label {new!r} already exists")
        renamed = self._backend.rename_label(existing[old], new)
        return Label(name=renamed.name, id=renamed.id)

    def delete_label(self, name: str) -> None:
        self._ns.require(name)
        self._backend.delete_label(self._require_existing(name))

    def _require_existing(self, name: str) -> str:
        existing = self._name_to_id()
        if name not in existing:
            raise ToolError(f"label {name!r} does not exist")
        return existing[name]
