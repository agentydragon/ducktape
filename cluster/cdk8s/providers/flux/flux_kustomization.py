"""Ergonomic wrapper for Flux's `Kustomization`, following cdk8s-plus's own construction
pattern: a class named after the kind, constructed as `Kustomization(scope, id, ...)`.
Every keyword is a `KustomizationSpec` field under its own name and type; `None` leaves
it unset, so Flux's own default applies. No artifact-derivation, ducktape namespace, or
policy default (interval/prune/wait) lives here -- those are
`cluster.cdk8s.flux.flux_kustomization`'s own.
"""

from __future__ import annotations

from collections.abc import Sequence

from constructs import Construct
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    Kustomization as _Kustomization,
    KustomizationSpec,
    KustomizationSpecBuildMetadata,
    KustomizationSpecCommonMetadata,
    KustomizationSpecDecryption,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthCheckExprs,
    KustomizationSpecHealthChecks,
    KustomizationSpecIgnore,
    KustomizationSpecImages,
    KustomizationSpecKubeConfig,
    KustomizationSpecPatches,
    KustomizationSpecPostBuild,
    KustomizationSpecSourceRef,
)

from cluster.cdk8s.metadata import metadata


class Kustomization(_Kustomization):
    """Flux's `Kustomization`. `source_ref`, `interval` and `prune` are the only fields
    the CRD itself requires; every other keyword is optional and Flux-defaulted when
    omitted."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        name: str,
        namespace: str,
        source_ref: KustomizationSpecSourceRef,
        interval: str,
        prune: bool,
        path: str | None = None,
        retry_interval: str | None = None,
        timeout: str | None = None,
        wait: bool | None = None,
        suspend: bool | None = None,
        deletion_policy: KustomizationSpecDeletionPolicy | None = None,
        decryption: KustomizationSpecDecryption | None = None,
        depends_on: Sequence[KustomizationSpecDependsOn] | None = None,
        health_checks: Sequence[KustomizationSpecHealthChecks] | None = None,
        health_check_exprs: Sequence[KustomizationSpecHealthCheckExprs] | None = None,
        post_build: KustomizationSpecPostBuild | None = None,
        target_namespace: str | None = None,
        service_account_name: str | None = None,
        images: Sequence[KustomizationSpecImages] | None = None,
        patches: Sequence[KustomizationSpecPatches] | None = None,
        build_metadata: Sequence[KustomizationSpecBuildMetadata] | None = None,
        common_metadata: KustomizationSpecCommonMetadata | None = None,
        components: Sequence[str] | None = None,
        force: bool | None = None,
        ignore: Sequence[KustomizationSpecIgnore] | None = None,
        ignore_missing_components: bool | None = None,
        kube_config: KustomizationSpecKubeConfig | None = None,
        name_prefix: str | None = None,
        name_suffix: str | None = None,
        annotations: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata(name, namespace, annotations=annotations),
            spec=KustomizationSpec(
                source_ref=source_ref,
                interval=interval,
                prune=prune,
                path=path,
                retry_interval=retry_interval,
                timeout=timeout,
                wait=wait,
                suspend=suspend,
                deletion_policy=deletion_policy,
                decryption=decryption,
                depends_on=list(depends_on) if depends_on is not None else None,
                health_checks=list(health_checks) if health_checks is not None else None,
                health_check_exprs=list(health_check_exprs) if health_check_exprs is not None else None,
                post_build=post_build,
                target_namespace=target_namespace,
                service_account_name=service_account_name,
                images=list(images) if images is not None else None,
                patches=list(patches) if patches is not None else None,
                build_metadata=list(build_metadata) if build_metadata is not None else None,
                common_metadata=common_metadata,
                components=list(components) if components is not None else None,
                force=force,
                ignore=list(ignore) if ignore is not None else None,
                ignore_missing_components=ignore_missing_components,
                kube_config=kube_config,
                name_prefix=name_prefix,
                name_suffix=name_suffix,
            ),
        )
