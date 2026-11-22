"""Consistency between Flux image automation and the webhook Receivers.

Every `ImageRepository` must be listed in some `Receiver`, so a registry webhook reconciles
it immediately — otherwise a new tag is only picked up on the 5-minute `ImageRepository`
poll. Also checks that no Receiver references one that no longer exists.

The Receiver check uses typed local renders from `ParsedCluster.rendered_resources`.
Local owners with `targetNamespace` use Flux dry-run. Other local owners use plain
Kustomize, which omits Flux-level patches. Both paths omit live decryption and
post-build variable sources.
"""

from __future__ import annotations

import re
from itertools import pairwise
from pathlib import Path

import yaml
from yaml.tokens import FlowMappingEndToken, FlowMappingStartToken

from cluster.validation.cluster import ParsedCluster
from cluster.validation.k8s import ImagePolicyResource, ImageRepositoryResource, ReceiverResource

# Flux's setter marker, e.g. `# {"$imagepolicy": "flux-system:haku-console"}` or the
# field-scoped `"flux-system:haku-console:tag"`.
_IMAGE_POLICY_MARKER = re.compile(r'"\$imagepolicy":\s*"([^"]+)"')

# ImageRepositories defined in Haku's haku-state repo, reconciled into
# haku-sandbox — not under cluster/k8s, so the validator can't see them. An operator-owned
# Receiver here may still reference one (cluster/generated/haku/ui-image-webhook), so exempt these
# from the "Receiver references an undefined ImageRepository" check.
_HAKU_STATE_IMAGE_REPOS = {"haku-sandbox/haku-anki", "haku-sandbox/haku-ui"}


def check_image_automation_webhook(cluster: ParsedCluster) -> list[str]:
    image_repos: set[str] = set()
    webhook_repos: set[str] = set()

    for resources in cluster.rendered_resources.values():
        for resource in resources:
            if isinstance(resource, ImageRepositoryResource):
                image_repos.add(f"{resource.namespace}/{resource.name}")
            elif isinstance(resource, ReceiverResource):
                webhook_repos.update(
                    f"{ref.namespace or resource.namespace}/{ref.name}"
                    for ref in resource.spec.resources
                    if ref.kind == "ImageRepository" and ref.name
                )

    return [
        *(
            f"ImageRepository '{name}' is not listed in any Receiver; new tags will only be picked up on the 5m poll, "
            "not on push. Add it to the Receiver for its registry (cluster/cdk8s/flux_webhook.py for GHCR, "
            "cluster/cdk8s/forgejo_registry/chart.py for the Forgejo registry)."
            for name in sorted(image_repos - webhook_repos)
        ),
        *(
            f"A Receiver references ImageRepository '{name}', but no such ImageRepository is defined in the cluster."
            for name in sorted(webhook_repos - image_repos - _HAKU_STATE_IMAGE_REPOS)
        ),
    ]


def check_image_policy_markers(cluster: ParsedCluster, k8s_dir: Path) -> list[str]:
    """Validate files Flux rewrites. A stale setter silently freezes the image (#3882).
    A nonempty flow mapping anywhere in the file makes Flux and Prettier alternate its
    spacing, breaking CI after image automation commits directly to devel.
    """
    policies = {
        f"{resource.namespace}:{resource.name}"
        for resources in cluster.rendered_resources.values()
        for resource in resources
        if isinstance(resource, ImagePolicyResource)
    }

    errors = []
    for path in sorted(k8s_dir.rglob("*.yaml")):
        text = path.read_text()
        # Setters are YAML comments. Only scan YAML tokens in files containing a setter.
        if not (refs := _IMAGE_POLICY_MARKER.findall(text)):
            continue
        relative = path.relative_to(k8s_dir)
        errors.extend(
            f"{relative} has an $imagepolicy marker for '{ref}', but no such "
            "ImagePolicy is defined under cluster/k8s. Flux will silently never update this image."
            for ref in refs
            # A field-scoped setter has a third component such as ":tag".
            if ":".join(ref.split(":")[:2]) not in policies
        )
        for opening, following in pairwise(yaml.scan(text, Loader=yaml.CSafeLoader)):
            if isinstance(opening, FlowMappingStartToken) and not isinstance(following, FlowMappingEndToken):
                errors.append(
                    f"{relative}:{opening.start_mark.line + 1} has a YAML flow mapping and an $imagepolicy "
                    "marker. Flux re-serialises the whole file when it updates the tag and will write it "
                    "without prettier's inner spaces, failing pre-commit on every open PR. Use block style."
                )
    return errors
