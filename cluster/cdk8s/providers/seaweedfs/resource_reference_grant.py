"""Ergonomic wrapper for the SeaweedFS operator's `ResourceReferenceGrant`, following cdk8s-plus's
own construction pattern: a class named after the kind, constructed as
`ResourceReferenceGrant(scope, id, ...)`. Both keywords are `ResourceReferenceGrantSpec` fields
under their own names and types. No ducktape namespace or cluster name lives here.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from seaweed_resourcereferencegrant_crds.com.seaweedfs.seaweed import (
    ResourceReferenceGrant as _ResourceReferenceGrant,
    ResourceReferenceGrantSpec,
    ResourceReferenceGrantSpecFrom,
    ResourceReferenceGrantSpecTo,
)


class ResourceReferenceGrant(_ResourceReferenceGrant):
    """Lets the objects `from_` lists reference, from their namespaces, the objects `to` lists in
    this grant's own namespace. Both are the fields the CRD itself requires."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        from_: Sequence[ResourceReferenceGrantSpecFrom],
        to: Sequence[ResourceReferenceGrantSpecTo],
    ) -> None:
        super().__init__(scope, id, metadata=metadata, spec=ResourceReferenceGrantSpec(from_=list(from_), to=list(to)))
