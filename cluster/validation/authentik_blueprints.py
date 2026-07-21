"""Authentik blueprint parsing and validation checks.

Authentik SSO is configured by native blueprints under
`cluster/k8s/authentik/app/blueprints/`. This module parses those blueprints —
including authentik's custom `!Find`/`!KeyOf` YAML tags, which `yaml.SafeLoader`
rejects — and holds the checks over them: that every blueprint file is wired into
the configMap, and that every proxy provider is assigned to an outpost.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


def check_blueprint_completeness(k8s_dir: Path) -> list[str]:
    """Check that all blueprint YAML files are listed in the authentik configMapGenerator."""
    authentik_kust = k8s_dir / "authentik" / "app" / "kustomization.yaml"
    blueprints_dir = k8s_dir / "authentik" / "app" / "blueprints"

    if not authentik_kust.exists():
        raise FileNotFoundError(f"Expected {authentik_kust} to exist")
    if not blueprints_dir.exists():
        raise FileNotFoundError(f"Expected {blueprints_dir} to exist")

    with authentik_kust.open() as f:
        doc = yaml.safe_load(f)

    listed_files: set[str] = set()
    for generator in doc.get("configMapGenerator", []):
        if generator.get("name") == "authentik-sso-blueprints":
            listed_files = {Path(f).name for f in generator.get("files", [])}
            break

    on_disk = {p.name for p in blueprints_dir.glob("*.yaml")}
    unlisted = sorted(on_disk - listed_files)

    if unlisted:
        return [
            f"Authentik blueprint not listed in configMapGenerator: {name}. "
            f"Add 'blueprints/{name}' to the authentik-sso-blueprints files list "
            f"in k8s/authentik/app/kustomization.yaml."
            for name in unlisted
        ]

    return []


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


def check_proxy_provider_outpost_assignment(k8s_dir: Path) -> list[str]:
    """Every `present` authentik proxy provider must be assigned to an outpost.

    A proxy provider that exists but is on no outpost has no host->backend mapping: the
    embedded outpost doesn't recognise its `external_host` and 302s to the Authentik login
    flow served *on that host*, so "Sign in with Google" builds the OAuth callback against
    the wrong host and Google rejects it with `redirect_uri_mismatch`. This guards the
    wiring that broke `haku.allegedly.works` (and, latently, `tandoor.allegedly.works`).
    """
    blueprints_dir = k8s_dir / "authentik" / "app" / "blueprints"
    if not blueprints_dir.exists():
        raise FileNotFoundError(f"Expected {blueprints_dir} to exist")

    entries = _blueprint_entries(blueprints_dir)

    # Map blueprint id -> provider name (for !KeyOf) and collect present provider names.
    id_to_name: dict[str, str] = {}
    present: set[str] = set()
    for entry in entries:
        if entry.model != _PROXY_PROVIDER_MODEL:
            continue
        name = entry.identifiers.name
        if name is None:
            continue
        if entry.id is not None:
            id_to_name[entry.id] = name
        if entry.state == "present":
            present.add(name)

    assigned = {
        name
        for entry in entries
        if entry.model == _OUTPOST_MODEL and entry.state == "present"
        for ref in entry.attrs.providers
        if (name := _resolve_provider_ref(ref, id_to_name)) is not None
    }

    return [
        f"Authentik proxy provider '{name}' is defined but assigned to no outpost. Add it to the "
        f"embedded outpost's providers in blueprints/embedded-outpost.yaml (see fava); "
        f"otherwise its host 302s to a login flow served on itself and Google SSO fails with "
        f"redirect_uri_mismatch."
        for name in sorted(present - assigned)
    ]
