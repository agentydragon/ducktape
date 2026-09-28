"""Ergonomic wrapper for trust-manager's `Bundle`, following cdk8s-plus's own construction
pattern: a class named after the kind, constructed as `Bundle(scope, id, ...)`. Every keyword is a
`BundleSpec` field under its own name and type; `None` leaves it unset, so trust-manager's own
default applies. No CA Secret, key or namespace fact lives here.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from trust_manager_crds.io.cert_manager.trust import Bundle as _Bundle, BundleSpec, BundleSpecSources, BundleSpecTarget


class Bundle(_Bundle):
    """trust-manager's cluster-scoped `Bundle`: `metadata` carries no `namespace`. `sources` is the
    only field the CRD itself requires."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        sources: Sequence[BundleSpecSources],
        target: BundleSpecTarget | None = None,
    ) -> None:
        super().__init__(scope, id, metadata=metadata, spec=BundleSpec(sources=list(sources), target=target))
