from collections.abc import Iterable
from typing import Any

from more_itertools import one


def find_resource(documents: Iterable[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    """The one document of `kind` named `name` in synthesized manifests; raises unless exactly one matches."""
    return one(doc for doc in documents if doc["kind"] == kind and doc["metadata"]["name"] == name)
