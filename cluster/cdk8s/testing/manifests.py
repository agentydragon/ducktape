from collections.abc import Iterable
from typing import Any

from more_itertools import one


def find_resource(
    documents: Iterable[dict[str, Any]], kind: str, name: str, *, namespace: str | None = None
) -> dict[str, Any]:
    """The one document of `kind` named `name` (in `namespace`, if given); raises unless exactly one matches.

    A document without `metadata`, such as a `kustomization.yaml`, never matches.
    """
    return one(
        doc
        for doc in documents
        if doc["kind"] == kind
        and doc.get("metadata", {}).get("name") == name
        and (namespace is None or doc["metadata"].get("namespace") == namespace)
    )
