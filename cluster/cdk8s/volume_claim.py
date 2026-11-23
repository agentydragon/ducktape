"""Builds a PersistentVolumeClaim from the values that vary between workloads."""

from __future__ import annotations

from collections.abc import Mapping

from cdk8s_plus_34 import k8s
from constructs import Construct


def volume_claim(
    scope: Construct,
    id: str,
    *,
    name: str,
    namespace: str,
    storage_class: str,
    size: str,
    access_mode: str = "ReadWriteOnce",
    annotations: Mapping[str, str] | None = None,
) -> k8s.KubePersistentVolumeClaim:
    """A claim of `size` on `storage_class`, ReadWriteOnce unless `access_mode` says otherwise.

    The storage class is a placement decision, so every caller states it.
    """
    return k8s.KubePersistentVolumeClaim(
        scope,
        id,
        metadata=k8s.ObjectMeta(name=name, namespace=namespace, annotations=dict(annotations) if annotations else None),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=[access_mode],
            storage_class_name=storage_class,
            resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string(size)}),
        ),
    )
