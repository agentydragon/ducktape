"""Derived schemas of the Agentplane CRDs, written by `bb run //x/agentplane/crds:generate_bin` and
pinned to the committed files by `:test_generate`.

A binding CRD's subject -- `EgressBinding.spec.subjects[]`, `ActionPolicyBinding.spec.subject` -- is
`x.agentplane.subjects.ServiceAccountRef`, the model both services decide against, so its schema block
is spliced into the otherwise hand-written CRD from the model's own JSON schema. Each served version's
`openAPIV3Schema` is then dumped verbatim to `cluster/schemas/<group>/<kind>_<version>.json`, where
the pre-commit kubeconform hook reads it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from textwrap import indent
from typing import Any

import yaml
from more_itertools import one
from pydantic import TypeAdapter

from util.bazel.workspace import get_build_workspace_directory
from x.agentplane.subjects import ServiceAccountRef

CRDS_DIR = Path("cluster/k8s/agentplane-crds")
SCHEMAS_DIR = Path("cluster/schemas")
CRD_FILES = (
    "crd-egresspolicies.yaml",
    "crd-egressbindings.yaml",
    "crd-egresscredentials.yaml",
    "crd-actionpolicysets.yaml",
    "crd-actionpolicybindings.yaml",
)
# The spec field holding a binding CRD's subject; an array field's items are the subject.
_SUBJECT_FIELDS = {"crd-egressbindings.yaml": "subjects", "crd-actionpolicybindings.yaml": "subject"}


class _FlowSequenceDumper(yaml.SafeDumper):
    """Block mappings over `[a, b]` sequences, the CRDs' house style."""


_FlowSequenceDumper.add_representer(
    list, lambda dumper, data: dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True)
)


def _crd_property(schema: dict[str, Any]) -> dict[str, Any]:
    kept = {key: value for key, value in schema.items() if key not in ("title", "additionalProperties")}
    return {"type": kept.pop("type"), **kept}


def subject_schema() -> dict[str, Any]:
    """`ServiceAccountRef` as a CRD structural schema.

    Dropped from the model's schema: `title`; `additionalProperties: false`, which the API server
    rejects in a CRD (it prunes unknown fields instead); and the model docstring, since each CRD
    describes the field in its binding's own terms.
    """
    model = TypeAdapter(ServiceAccountRef).json_schema()
    if "$defs" in model:
        raise ValueError("ServiceAccountRef nests a model, and a CRD schema cannot hold a $ref")
    return {
        "type": model["type"],
        "required": model["required"],
        "properties": {name: _crd_property(prop) for name, prop in model["properties"].items()},
    }


def _child(node: yaml.Node, key: str) -> yaml.Node:
    pairs: list[tuple[yaml.Node, yaml.Node]] = node.value
    return one(value for key_node, value in pairs if key_node.value == key)


def _subject_node(crd: yaml.Node, field: str) -> yaml.Node:
    versions: list[yaml.Node] = _child(_child(crd, "spec"), "versions").value
    node = one(versions)
    for key in ("schema", "openAPIV3Schema", "properties", "spec", "properties", field):
        node = _child(node, key)
    return _child(node, "items") if _child(node, "type").value == "array" else node


def with_subject_schema(crd_text: str, field: str) -> str:
    """`crd_text` with the subject block under `spec.<field>` (its items, for an array) regenerated.

    The block is the trailing keys of that mapping; keys written by hand, such as `description`,
    precede it and stay.
    """
    node = _subject_node(yaml.compose(crd_text), field)
    generated = subject_schema()
    keys = [key_node.value for key_node, _ in node.value]
    first = min(keys.index(key) for key in generated)
    if extra := set(keys[first:]) - set(generated):
        raise ValueError(f"{field}: hand-written keys {extra} sit inside the generated block")
    start = node.value[first][0].start_mark
    lines = crd_text.splitlines(keepends=True)
    block = yaml.dump(generated, Dumper=_FlowSequenceDumper, sort_keys=False)
    return "".join(lines[: start.line]) + indent(block, " " * start.column) + "".join(lines[node.end_mark.line :])


def generated_files(crds: Mapping[str, str]) -> Iterator[tuple[Path, str]]:
    """(repo-relative path, content) of every generated file, from each CRD file's text by name."""
    for crd_file, committed in crds.items():
        if field := _SUBJECT_FIELDS.get(crd_file):
            text = with_subject_schema(committed, field)
            yield CRDS_DIR / crd_file, text
        else:
            text = committed
        crd = yaml.safe_load(text)
        group, kind = crd["spec"]["group"], crd["spec"]["names"]["kind"].lower()
        for version in crd["spec"]["versions"]:
            schema = json.dumps(version["schema"]["openAPIV3Schema"], indent=2, ensure_ascii=False)
            yield SCHEMAS_DIR / group / f"{kind}_{version['name']}.json", schema + "\n"


def main() -> None:
    root = get_build_workspace_directory()
    for relative, content in generated_files({name: (root / CRDS_DIR / name).read_text() for name in CRD_FILES}):
        (root / relative).write_text(content)


if __name__ == "__main__":
    main()
