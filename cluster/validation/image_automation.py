"""Consistency between Flux image automation and the GitHub webhook receiver.

Every `ImageRepository` must be listed in the `flux-webhook` GitHub `Receiver` so a push /
`registry_package` webhook reconciles it immediately — otherwise new GHCR tags are only
picked up on the 5-minute `ImageRepository` poll. Also checks that every `ImagePolicy`
references a defined `ImageRepository`, and that the webhook doesn't reference one that no
longer exists.

Operates on the kustomize build output (`ParsedCluster.build_results`) — the rendered
manifests the rest of the validator already produces — rather than re-walking source YAML.
That keeps it consistent with the other checks, sees exactly what Flux applies, and avoids
choking on the Authentik blueprints' custom `!Find`/`!Env` tags (which only appear as
ConfigMap string data once rendered, never as document-level YAML tags).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from cluster.validation.cluster import ParsedCluster


class _Meta(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = ""


class _Manifest(BaseModel):
    """Just enough of a rendered manifest to route it by kind."""

    model_config = ConfigDict(extra="ignore")
    kind: str = ""
    metadata: _Meta = Field(default_factory=_Meta)


class _RepoRef(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = ""


class _ImagePolicySpec(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)
    image_repository_ref: _RepoRef = Field(default_factory=_RepoRef)


class _ResourceRef(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: str = ""
    name: str = ""


class _ReceiverSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")
    resources: list[_ResourceRef] = []


def check_image_automation_webhook(cluster: ParsedCluster) -> list[str]:
    image_repos: set[str] = set()
    policy_refs: dict[str, str] = {}
    webhook_repos: set[str] = set()

    for result in cluster.build_results:
        for doc in result.docs:
            manifest = _Manifest.model_validate(doc)
            spec = doc.get("spec") or {}
            if manifest.kind == "ImageRepository":
                image_repos.add(manifest.metadata.name)
            elif manifest.kind == "ImagePolicy":
                policy_refs[manifest.metadata.name] = _ImagePolicySpec.model_validate(spec).image_repository_ref.name
            elif manifest.kind == "Receiver":
                receiver = _ReceiverSpec.model_validate(spec)
                webhook_repos.update(r.name for r in receiver.resources if r.kind == "ImageRepository" and r.name)

    return [
        *(
            f"ImageRepository '{name}' is not listed in flux-webhook/github-webhook-receiver.yaml; "
            "new GHCR tags will only be picked up on the 5m poll, not on push. Add it to the Receiver's resources."
            for name in sorted(image_repos - webhook_repos)
        ),
        *(
            f"flux-webhook/github-webhook-receiver.yaml references ImageRepository '{name}', "
            "but no such ImageRepository is defined under cluster/k8s."
            for name in sorted(webhook_repos - image_repos)
        ),
        *(
            f"ImagePolicy '{policy}' references ImageRepository '{ref}', which is not defined."
            for policy, ref in sorted(policy_refs.items())
            if ref not in image_repos
        ),
    ]
