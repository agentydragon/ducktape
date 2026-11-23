"""API objects in a cdk8s construct tree."""

from collections.abc import Iterator
from typing import cast

from cdk8s import ApiObject
from constructs import Construct


def api_objects_in(scope: Construct) -> Iterator[ApiObject]:
    """Include resources nested inside components; Chart.api_objects lists only direct children."""
    for node in scope.node.find_all():
        # Typed Python objects already identify themselves. Ask JSII about generic proxies.
        if isinstance(node, ApiObject) or ApiObject.is_api_object(node):
            yield cast(ApiObject, node)
