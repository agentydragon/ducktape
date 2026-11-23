"""Contracts for the Authentik source blueprints."""

from __future__ import annotations

import dataclasses

import yaml
from pydantic import BaseModel, ConfigDict, Field
from python.runfiles import Runfiles

_BLUEPRINTS_DIR = "ducktape/cluster/k8s/authentik/app/blueprints"


class _BlueprintLoader(yaml.SafeLoader):
    """SafeLoader that constructs authentik's custom `!`-tags instead of erroring on them.

    `yaml.SafeLoader` rejects authentik's local `!`-tags outright. We interpret only the
    cross-references that wire providers to outposts (`!Find`, `!KeyOf`); any other tag
    (`!Env`, `!Format`, …) is constructed to its raw argument, which lands in a field this
    check ignores.
    """


@dataclasses.dataclass(frozen=True)
class _Find:
    """authentik `!Find [model, [field, value]]` — resolve an object by a field match."""

    model: str
    field: str
    value: str


@dataclasses.dataclass(frozen=True)
class _KeyOf:
    """authentik `!KeyOf <entry-id>` — reference another blueprint entry by its `id`."""

    entry_id: str


def _construct_blueprint_tag(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> object:
    # Construct the node's children by type — never re-dispatch this node's own tag (infinite recursion).
    if isinstance(node, yaml.ScalarNode):
        arg: object = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        arg = loader.construct_sequence(node, deep=True)
    elif isinstance(node, yaml.MappingNode):
        arg = loader.construct_mapping(node, deep=True)
    else:
        raise TypeError(f"unexpected YAML node type: {type(node)}")
    match tag_suffix, arg:
        case "KeyOf", str(entry_id):
            return _KeyOf(entry_id)
        case "Find", [str(model), [str(field), str(value)]]:
            return _Find(model, field, value)
        case _:
            return arg


_BlueprintLoader.add_multi_constructor("!", _construct_blueprint_tag)

_PROXY_PROVIDER_MODEL = "authentik_providers_proxy.proxyprovider"
_OUTPOST_MODEL = "authentik_outposts.outpost"


class _Identifiers(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str | None = None


class _EntryAttrs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    # Outpost provider refs are `_Find`/`_KeyOf` instances; other tags/values are ignored by the reader.
    providers: list[object] = Field(default_factory=list)


class _BlueprintEntry(BaseModel):
    """One `entries:` item of an authentik blueprint (only the fields this check reads)."""

    model_config = ConfigDict(extra="ignore", protected_namespaces=())
    model: str
    id: str | None = None
    state: str = "present"
    identifiers: _Identifiers = Field(default_factory=_Identifiers)
    attrs: _EntryAttrs = Field(default_factory=_EntryAttrs)


def _resolve_provider_ref(ref: object, id_to_name: dict[str, str]) -> str | None:
    """Resolve one outpost `providers` reference to a proxy-provider name."""
    if isinstance(ref, _KeyOf):
        return id_to_name.get(ref.entry_id)
    if isinstance(ref, _Find) and ref.model == _PROXY_PROVIDER_MODEL and ref.field == "name":
        return ref.value
    return None


def test_proxy_providers_and_outpost_assignments(runfiles: Runfiles) -> None:
    """An unassigned provider sends login to the wrong host (haku's redirect_uri_mismatch).
    Providers owned outside blueprints may be assigned by name. Tombstoned ones may not.
    """
    directory = runfiles.root() / _BLUEPRINTS_DIR
    paths = sorted(directory.glob("*.yaml"))
    assert paths, directory
    present: set[str] = set()
    absent: set[str] = set()
    assigned: set[str] = set()
    for path in paths:
        for doc in yaml.load_all(path.read_text(), Loader=_BlueprintLoader):
            if not isinstance(doc, dict):
                continue
            # Authentik resolves !KeyOf to an instance already applied in this blueprint.
            id_to_name: dict[str, str] = {}
            for raw in doc.get("entries", []):
                entry = _BlueprintEntry.model_validate(raw)
                if entry.model == _PROXY_PROVIDER_MODEL and (name := entry.identifiers.name) is not None:
                    if entry.state == "absent":
                        absent.add(name)
                    elif entry.state in {"present", "created", "must_created"}:
                        present.add(name)
                        if entry.id is not None:
                            id_to_name[entry.id] = name
                elif entry.model == _OUTPOST_MODEL and entry.state == "present":
                    for ref in entry.attrs.providers:
                        provider = _resolve_provider_ref(ref, id_to_name)
                        assert provider is not None, f"{path.name}: unresolved outpost provider {ref!r}"
                        assigned.add(provider)

    assert not (present - assigned), f"Proxy providers without an outpost: {sorted(present - assigned)}"
    assert not (absent & assigned), f"Outposts reference retired proxy providers: {sorted(absent & assigned)}"
