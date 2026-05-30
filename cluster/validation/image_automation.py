"""Consistency between Flux image automation and the GitHub webhook receiver.

Every `ImageRepository` must be listed in the `flux-webhook` GitHub `Receiver` so a push /
`registry_package` webhook reconciles it immediately — otherwise new GHCR tags are only
picked up on the 5-minute `ImageRepository` poll. Also checks that every `ImagePolicy`
references a defined `ImageRepository`, and that the webhook doesn't reference one that no
longer exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def _docs(path: Path) -> list[Any]:
    if not path.exists():
        return []
    return [d for d in yaml.safe_load_all(path.read_text()) if isinstance(d, dict) and d.get("kind")]


def check_image_automation_webhook(root: Path) -> list[str]:
    image_repos: set[str] = set()
    policy_refs: dict[str, Any] = {}
    for f in sorted(root.rglob("*.yaml")):
        if f.name.endswith(".sops.yaml"):
            continue
        for doc in _docs(f):
            if doc["kind"] == "ImageRepository":
                image_repos.add(doc.get("metadata", {}).get("name"))
            elif doc["kind"] == "ImagePolicy":
                policy_refs[doc.get("metadata", {}).get("name")] = (
                    doc.get("spec", {}).get("imageRepositoryRef", {}).get("name")
                )

    webhook_repos: set[str] = set()
    for doc in _docs(root / "flux-webhook" / "github-webhook-receiver.yaml"):
        if doc["kind"] == "Receiver":
            for ref in doc.get("spec", {}).get("resources", []):
                if isinstance(ref, dict) and ref.get("kind") == "ImageRepository":
                    name = ref.get("name")
                    if name:
                        webhook_repos.add(name)

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
