"""Ergonomic wrapper for KubeVirt's `VirtualMachine`, following cdk8s-plus's own construction
pattern: a class named after the kind, with keyword parameters mirroring `VirtualMachineSpec`'s
own fields.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata
from constructs import Construct
from kubevirt_virtualmachine_crds.io.kubevirt import (
    VirtualMachine as _VirtualMachine,
    VirtualMachineSpec,
    VirtualMachineSpecTemplate,
)


class VirtualMachine(_VirtualMachine):
    """`template` is the only field the CRD requires. `None` leaves an optional field unset, so
    KubeVirt's own default applies."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        template: VirtualMachineSpecTemplate,
        run_strategy: str | None = None,
    ) -> None:
        super().__init__(
            scope, id, metadata=metadata, spec=VirtualMachineSpec(template=template, run_strategy=run_strategy)
        )
