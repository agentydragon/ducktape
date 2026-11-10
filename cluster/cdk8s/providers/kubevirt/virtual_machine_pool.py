"""Ergonomic wrapper for KubeVirt's `VirtualMachinePool`, following cdk8s-plus's own construction
pattern: a class named after the kind, with keyword parameters mirroring `VirtualMachinePoolSpec`'s
own fields.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata
from constructs import Construct
from kubevirt_virtualmachinepool_crds.io.kubevirt.pool import (
    VirtualMachinePool as _VirtualMachinePool,
    VirtualMachinePoolSpec,
    VirtualMachinePoolSpecSelector,
    VirtualMachinePoolSpecVirtualMachineTemplate,
)


class VirtualMachinePool(_VirtualMachinePool):
    """`selector` and `virtual_machine_template` are the only fields the CRD requires; the selector
    must match the labels of `virtual_machine_template`'s metadata. `None` leaves an optional field
    unset, so KubeVirt's own default applies (a pool created without `replicas` runs one VM)."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        selector: VirtualMachinePoolSpecSelector,
        virtual_machine_template: VirtualMachinePoolSpecVirtualMachineTemplate,
        replicas: int | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=VirtualMachinePoolSpec(
                selector=selector, virtual_machine_template=virtual_machine_template, replicas=replicas
            ),
        )
