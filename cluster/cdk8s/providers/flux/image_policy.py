"""Ergonomic wrapper for Flux's `ImagePolicy`, following cdk8s-plus's own construction
pattern: a class named after the kind, constructed as `ImagePolicy(scope, id, ...)`.
Every keyword is an `ImagePolicySpec` field under its own name and type; `None` leaves it
unset, so Flux's own default applies. No ducktape tag scheme or namespace lives here.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata
from constructs import Construct
from flux_imagepolicy_crds.io.fluxcd.toolkit.image import (
    ImagePolicy as _ImagePolicy,
    ImagePolicySpec,
    ImagePolicySpecDigestReflectionPolicy,
    ImagePolicySpecFilterTags,
    ImagePolicySpecImageRepositoryRef,
    ImagePolicySpecPolicy,
)


class ImagePolicy(_ImagePolicy):
    """Flux's `ImagePolicy`. `image_repository_ref` and `policy` are the only fields the CRD
    itself requires. `interval` is set exactly when `digest_reflection_policy` is `Always`,
    the combination the CRD's validation admits; any other raises here instead of at apply.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        image_repository_ref: ImagePolicySpecImageRepositoryRef,
        policy: ImagePolicySpecPolicy,
        filter_tags: ImagePolicySpecFilterTags | None = None,
        digest_reflection_policy: ImagePolicySpecDigestReflectionPolicy | None = None,
        interval: str | None = None,
        suspend: bool | None = None,
    ) -> None:
        if (interval is not None) != (digest_reflection_policy == ImagePolicySpecDigestReflectionPolicy.ALWAYS):
            raise ValueError(f"{id=}: interval goes with digest_reflection_policy=Always: {interval=}")
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=ImagePolicySpec(
                image_repository_ref=image_repository_ref,
                policy=policy,
                filter_tags=filter_tags,
                digest_reflection_policy=digest_reflection_policy,
                interval=interval,
                suspend=suspend,
            ),
        )
