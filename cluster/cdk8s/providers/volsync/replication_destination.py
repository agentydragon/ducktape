"""Ergonomic wrapper for VolSync's `ReplicationDestination`, following cdk8s-plus's own
construction pattern: a class named after the kind, with `ReplicationDestinationSpec`'s
fields promoted onto its own `__init__`.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata
from constructs import Construct
from volsync_replicationdestination_crds.backube.volsync import (
    ReplicationDestination as _ReplicationDestination,
    ReplicationDestinationSpec,
    ReplicationDestinationSpecRsyncTls,
    ReplicationDestinationSpecTrigger,
)


class ReplicationDestination(_ReplicationDestination):
    """VolSync `ReplicationDestination`. `rsync_tls` is `spec.rsyncTLS` -- the only backend
    this repo builds a `ReplicationDestination` for today; `trigger=None` leaves the
    destination listening continuously rather than gated on a manual trigger key.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        rsync_tls: ReplicationDestinationSpecRsyncTls,
        trigger: ReplicationDestinationSpecTrigger | None = None,
    ) -> None:
        super().__init__(
            scope, id, metadata=metadata, spec=ReplicationDestinationSpec(rsync_tls=rsync_tls, trigger=trigger)
        )
