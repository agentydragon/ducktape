#!/usr/bin/env python3
"""Assert every in-cluster GHCR image is anonymously pullable (i.e. public).

New GHCR packages default to private, and GitHub exposes no API to set package
visibility, so a new in-cluster image silently ``ImagePullBackOff``s on the
credential-less Talos nodes until someone flips it to public by hand. This
detects that and fails loudly (it cannot auto-fix).

SSOT for "which images must be public" is the ``ImageRepository`` manifests under
``cluster/k8s/flux-image-automation-ghcr/`` (their ``spec.image``), filtered to
``ghcr.io``. We test anonymous pullability against the registry rather than the
packages API: the packages API requires auth even for public packages, whereas an
anonymous ``ghcr.io`` pull token grants ``tags/list`` only for a public package —
which is exactly the property the nodes rely on, and needs no secret.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

REGISTRY = "ghcr.io"
MANIFEST_DIR = Path("cluster/k8s/flux-image-automation-ghcr")
TIMEOUT = 15


def ghcr_packages(owner: str) -> list[str]:
    """Package names (``spec.image`` minus the ``ghcr.io/<owner>/`` prefix)."""
    prefix = f"{REGISTRY}/{owner}/"
    packages: set[str] = set()
    for path in sorted(MANIFEST_DIR.glob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if not isinstance(doc, dict) or doc.get("kind") != "ImageRepository":
                continue
            image = doc.get("spec", {}).get("image", "")
            if image.startswith(prefix):
                packages.add(image.removeprefix(prefix))
    return sorted(packages)


def anon_pullable(owner: str, pkg: str) -> tuple[bool, str]:
    """Whether ``pkg`` is anonymously pullable from GHCR (public).

    A private or not-yet-pushed package is denied at either step: the anonymous
    token request itself 403s, or the granted token lacks pull scope so
    ``tags/list`` 403s.
    """
    token_url = f"https://{REGISTRY}/token?scope=repository:{owner}/{pkg}:pull"
    try:
        with urllib.request.urlopen(token_url, timeout=TIMEOUT) as resp:
            token = json.load(resp)["token"]
        req = urllib.request.Request(
            f"https://{REGISTRY}/v2/{owner}/{pkg}/tags/list", headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status == 200, str(resp.status)
    except urllib.error.HTTPError as err:
        return False, str(err.code)


def main() -> int:
    owner = os.environ.get("OWNER") or "agentydragon"
    packages = ghcr_packages(owner)
    if not packages:
        print(f"No {REGISTRY}/{owner} images found under {MANIFEST_DIR}", file=sys.stderr)
        return 1

    print(f"Checking {len(packages)} in-cluster GHCR packages for {owner}:")
    bad: list[str] = []
    for pkg in packages:
        ok, code = anon_pullable(owner, pkg)
        print(f"  {'ok    ' if ok else 'NOT OK'}  {pkg}{'' if ok else f' (http {code})'}")
        if not ok:
            bad.append(pkg)

    if not bad:
        print(f"All {len(packages)} packages are anonymously pullable. ✅")
        return 0

    report = "\n".join(
        [
            "### ❌ GHCR packages not anonymously pullable",
            "",
            "Cluster nodes pull without credentials, so each must be public",
            "(or, if brand new, pushed once then flipped public).",
            "GitHub has no API to set visibility — fix each in the UI:",
            "",
            *(f"- `{p}` — https://github.com/users/{owner}/packages/container/{p}/settings" for p in bad),
        ]
    )
    print("\n" + report, file=sys.stderr)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a") as fh:
            fh.write(report + "\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
