"""RBAC rule resources for kinds cdk8s_plus_34 has no constant for."""

from __future__ import annotations

from typing import cast

from cdk8s_plus_34 import ApiResource, IApiResource


def custom_resource(api_group: str, resource_type: str) -> IApiResource:
    # cdk8s_plus_34's Python stub doesn't declare ApiResource as implementing IApiResource
    # (TS's `@jsii.implements(IApiResource, ...)` on the class doesn't reach the generated
    # .pyi), though every instance satisfies it at runtime with `resource_name` None.
    return cast(IApiResource, ApiResource.custom(api_group=api_group, resource_type=resource_type))
