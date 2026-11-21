"""Contracts for the Authentik source blueprints."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import pytest_bazel
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


def _blueprint_entries(blueprints_dir: Path) -> list[_BlueprintEntry]:
    """Parse every blueprint document's `entries:` into typed models."""
    return [
        _BlueprintEntry.model_validate(entry)
        for path in sorted(blueprints_dir.glob("*.yaml"))
        for doc in yaml.load_all(path.read_text(), Loader=_BlueprintLoader)
        if isinstance(doc, dict)
        for entry in doc.get("entries", [])
        if isinstance(entry, dict)
    ]


@dataclasses.dataclass(frozen=True)
class _Wiring:
    """Proxy-provider names by blueprint `state`, and each `present` outpost's `providers` resolved to names.

    A reference that resolves to no proxy-provider name is `None`.
    """

    present: set[str]
    absent: set[str]
    outposts: list[list[str | None]]


@pytest.fixture(scope="session")
def wiring(runfiles: Runfiles) -> _Wiring:
    blueprints_dir = runfiles.root() / _BLUEPRINTS_DIR
    if not blueprints_dir.exists():
        raise FileNotFoundError(f"Expected {blueprints_dir} to exist")
    entries = _blueprint_entries(blueprints_dir)
    id_to_name: dict[str, str] = {}
    present: set[str] = set()
    absent: set[str] = set()
    for entry in entries:
        name = entry.identifiers.name
        if entry.model != _PROXY_PROVIDER_MODEL or name is None:
            continue
        if entry.id is not None:
            id_to_name[entry.id] = name
        if entry.state == "present":
            present.add(name)
        elif entry.state == "absent":
            absent.add(name)
    return _Wiring(
        present=present,
        absent=absent,
        outposts=[
            [_resolve_provider_ref(ref, id_to_name) for ref in entry.attrs.providers]
            for entry in entries
            if entry.model == _OUTPOST_MODEL and entry.state == "present"
        ],
    )


def test_proxy_providers_assigned_to_outpost(wiring: _Wiring) -> None:
    """Every `present` authentik proxy provider must be assigned to an outpost.

    A proxy provider that exists but is on no outpost has no host->backend mapping: the
    embedded outpost doesn't recognise its `external_host` and 302s to the Authentik login
    flow served *on that host*, so "Sign in with Google" builds the OAuth callback against
    the wrong host and Google rejects it with `redirect_uri_mismatch`. This guards the
    wiring that broke `haku.allegedly.works` (and, latently, `tandoor.allegedly.works`).
    """
    assigned = {name for outpost in wiring.outposts for name in outpost}
    unassigned = sorted(wiring.present - assigned)
    assert not unassigned, (
        f"Authentik proxy providers {unassigned} are defined but assigned to no outpost. Add them to the "
        "embedded outpost's providers in blueprints/embedded-outpost.yaml (see goldilocks)."
    )


def test_outpost_provider_references_resolve(wiring: _Wiring) -> None:
    """Embedded-outpost refs are valid and do not target retired providers.

    Authentik accepts a blueprint with a dangling ``!KeyOf`` reference and only
    reports the broken outpost wiring during reconciliation. Provider names that
    are not defined in blueprints are allowed because the ownership split also has
    Terraform-managed providers; explicit ``state: absent`` tombstones are not.
    """
    for outpost in wiring.outposts:
        unresolved = [index for index, name in enumerate(outpost) if name is None]
        assert not unresolved, (
            f"Authentik outpost provider references {unresolved} do not resolve to a proxy-provider name "
            "or blueprint entry."
        )
        retired = sorted(wiring.absent.intersection(outpost))
        assert not retired, (
            f"Authentik outpost references retired proxy providers {retired}. Remove them from embedded-outpost.yaml."
        )


if __name__ == "__main__":
    pytest_bazel.main()
