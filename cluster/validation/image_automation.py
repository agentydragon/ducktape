"""Consistency between Flux image automation and the GitHub webhook receiver.

Every `ImageRepository` must be listed in the `flux-webhook` GitHub `Receiver` so a push /
`registry_package` webhook reconciles it immediately — otherwise new GHCR tags are only
picked up on the 5-minute `ImageRepository` poll. Also checks that every `ImagePolicy`
references a defined `ImageRepository`, and that the webhook doesn't reference one that no
longer exists.

Reads the typed resources from `ParsedCluster.build_results` — the kustomize/flux build
output the validator already produces — and isinstance-dispatches on the parsed variants, so
it sees exactly what Flux applies and never re-walks source YAML (which would choke on the
Authentik blueprints' custom `!Find`/`!Env` tags).
"""

from __future__ import annotations

import re
from pathlib import Path

from cluster.validation.cluster import ParsedCluster
from cluster.validation.k8s import ImagePolicyResource, ImageRepositoryResource, ReceiverResource

# Flux's setter marker, e.g. `# {"$imagepolicy": "flux-system:haku-console"}` or the
# field-scoped `"flux-system:haku-console:tag"`.
_IMAGE_POLICY_MARKER = re.compile(r'"\$imagepolicy":\s*"([^"]+)"')

# ImageRepositories defined in Haku's haku-state repo, reconciled into
# haku-sandbox — not under cluster/k8s, so the validator can't see them. An operator-owned
# Receiver here may still reference one (cluster/k8s/haku/ui-image-webhook), so exempt these
# from the "Receiver references an undefined ImageRepository" check.
_HAKU_STATE_IMAGE_REPOS = {"haku-anki", "haku-ui"}


def check_image_automation_webhook(cluster: ParsedCluster) -> list[str]:
    image_repos: set[str] = set()
    # Only GHCR images get push-time reconcile via the GitHub `registry_package`
    # webhook; images in other registries (e.g. our own Forgejo, git.allegedly.works)
    # can't, so they're not required in the GitHub Receiver — they use the 5m poll
    # (or their own registry webhook).
    ghcr_repos: set[str] = set()
    policy_refs: dict[str, str] = {}
    webhook_repos: set[str] = set()

    for result in cluster.build_results:
        for resource in result.resources:
            if isinstance(resource, ImageRepositoryResource):
                image_repos.add(resource.name)
                # The registry host is the first path component of the OCI ref.
                # Compare it exactly (not a substring/prefix match) so a repo like
                # `git.allegedly.works/…` can't be mistaken for GHCR.
                if resource.spec.image.split("/", 1)[0] == "ghcr.io":
                    ghcr_repos.add(resource.name)
            elif isinstance(resource, ImagePolicyResource):
                policy_refs[resource.name] = resource.spec.image_repository_ref.name
            elif isinstance(resource, ReceiverResource):
                webhook_repos.update(
                    ref.name for ref in resource.spec.resources if ref.kind == "ImageRepository" and ref.name
                )

    return [
        *(
            f"ImageRepository '{name}' is not listed in flux-webhook/github-webhook-receiver.yaml; "
            "new GHCR tags will only be picked up on the 5m poll, not on push. Add it to the Receiver's resources."
            for name in sorted(ghcr_repos - webhook_repos)
        ),
        *(
            f"flux-webhook/github-webhook-receiver.yaml references ImageRepository '{name}', "
            "but no such ImageRepository is defined under cluster/k8s."
            for name in sorted(webhook_repos - image_repos - _HAKU_STATE_IMAGE_REPOS)
        ),
        *(
            f"ImagePolicy '{policy}' references ImageRepository '{ref}', which is not defined."
            for policy, ref in sorted(policy_refs.items())
            if ref not in image_repos
        ),
    ]


def check_image_policy_markers(cluster: ParsedCluster, k8s_dir: Path) -> list[str]:
    """Every `$imagepolicy` setter marker names an ImagePolicy that exists.

    The marker is a YAML *comment*, so it survives into none of the parsed resources and
    nothing else here can see it: a typo'd or renamed policy name leaves the marker inert,
    Flux silently stops updating that image, and the deployment freezes on whatever tag it
    last had. That reads as "this image just isn't being rebuilt" — which is how
    `claude-iron-deployment.yaml` sat un-updated (PR #3882).

    Matched by regex over the source text rather than by parsing, deliberately: the
    Authentik blueprints' custom `!Find`/`!Env` tags choke a YAML walk of this tree.
    """
    policies = {
        f"{resource.namespace}:{resource.name}"
        for result in cluster.build_results
        for resource in result.resources
        if isinstance(resource, ImagePolicyResource)
    }

    return [
        f"{path.relative_to(k8s_dir)} has an $imagepolicy marker for '{ref}', but no such "
        "ImagePolicy is defined under cluster/k8s. Flux will silently never update this image."
        for path in sorted(k8s_dir.rglob("*.yaml"))
        for ref in _IMAGE_POLICY_MARKER.findall(path.read_text())
        # A marker may name a field (`namespace:name:tag`); the policy is the first two parts.
        if ":".join(ref.split(":")[:2]) not in policies
    ]
