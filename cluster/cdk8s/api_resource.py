"""RBAC rule resources for kinds cdk8s_plus_34 has no constant for."""

from __future__ import annotations

from typing import cast

import jsii
from cdk8s_plus_34 import ApiResource, IApiEndpoint, IApiResource


def custom_resource(api_group: str, resource_type: str) -> IApiResource:
    # cdk8s_plus_34's Python stub doesn't declare ApiResource as implementing IApiResource
    # (TS's `@jsii.implements(IApiResource, ...)` on the class doesn't reach the generated
    # .pyi), though every instance satisfies it at runtime with `resource_name` None.
    return cast(IApiResource, ApiResource.custom(api_group=api_group, resource_type=resource_type))


@jsii.implements(IApiResource, IApiEndpoint)
class _NamedApiResource:
    """An IApiResource naming one object for a resourceNames-scoped RBAC rule."""

    def __init__(self, *, api_group: str, resource_type: str, resource_name: str) -> None:
        self._api_group = api_group
        self._resource_type = resource_type
        self._resource_name = resource_name

    def as_api_resource(self) -> IApiResource:
        return self

    def as_non_api_resource(self) -> str | None:
        return None

    @property
    def api_group(self) -> str:
        return self._api_group

    @property
    def resource_type(self) -> str:
        return self._resource_type

    @property
    def resource_name(self) -> str | None:
        return self._resource_name


def named_resource(api_group: str, resource_type: str, resource_name: str) -> _NamedApiResource:
    """Return the named API resource shape cdk8s-plus lacks for this RBAC rule."""
    return _NamedApiResource(api_group=api_group, resource_type=resource_type, resource_name=resource_name)
