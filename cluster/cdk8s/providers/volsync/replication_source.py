"""Ergonomic wrapper for VolSync's `ReplicationSource`, following cdk8s-plus's own
construction pattern: a class named after the kind, dispatching `mover` onto whichever of
`ReplicationSourceSpec`'s mutually exclusive backend fields it was built for.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata
from constructs import Construct
from volsync_replicationsource_crds.backube.volsync import (
    ReplicationSource as _ReplicationSource,
    ReplicationSourceSpec,
    ReplicationSourceSpecRestic,
    ReplicationSourceSpecRsyncTls,
    ReplicationSourceSpecTrigger,
)


class ReplicationSource(_ReplicationSource):
    """VolSync `ReplicationSource`. `mover` selects the sync backend -- this repo uses
    `ReplicationSourceSpecRsyncTls` for VolSync-to-VolSync transfers into a matching
    `ReplicationDestination`, and `ReplicationSourceSpecRestic` for encrypted, deduplicated
    snapshots into an external repository. VolSync treats the two as mutually exclusive; the
    CRD schema doesn't enforce that, so dispatch here rather than setting both raw fields.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        source_pvc: str,
        trigger: ReplicationSourceSpecTrigger,
        mover: ReplicationSourceSpecRsyncTls | ReplicationSourceSpecRestic,
    ) -> None:
        if isinstance(mover, ReplicationSourceSpecRsyncTls):
            spec = ReplicationSourceSpec(source_pvc=source_pvc, trigger=trigger, rsync_tls=mover)
        elif isinstance(mover, ReplicationSourceSpecRestic):
            spec = ReplicationSourceSpec(source_pvc=source_pvc, trigger=trigger, restic=mover)
        else:
            raise ValueError(f"unsupported mover: {mover!r}")
        super().__init__(scope, id, metadata=metadata, spec=spec)
