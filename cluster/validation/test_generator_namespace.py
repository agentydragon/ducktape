"""A kustomization with a generator must declare a namespace.

`configMapGenerator` and `secretGenerator` produce resources with no namespace of
their own. Hand-written manifests in these directories carry an explicit
`metadata.namespace`, so the omission is invisible until apply time, when the
server rejects the generated object with

    ConfigMap/<name> namespace not specified:
      the server could not find the requested resource

Flux then applies *nothing* from that directory. That is the failure mode this
guards: on 2026-07-29 it left the public-coder-agent instance serving 502s for
three hours on a stale Deployment, and it was invisible from the manifests --
every file that had a namespace had the right one, and the one that needed the
kustomization to supply it was generated.

The fix in every case is `namespace: <ns>` at the top of the kustomization.
Kustomize then stamps it onto generated and hand-written resources alike, and
`kubeconform` cannot catch the omission because the generated object does not
exist as a file to lint.
"""

from pathlib import Path

import pytest
import pytest_bazel
import yaml

from util.bazel.runfiles import get_required_path

_K8S_ROOT_KUSTOMIZATION = "_main/cluster/k8s/kustomization.yaml"

GENERATOR_FIELDS = ("configMapGenerator", "secretGenerator")


@pytest.fixture(scope="session")
def k8s_dir() -> Path:
    return get_required_path(_K8S_ROOT_KUSTOMIZATION).parent


def _flux_target_namespace(kust: Path) -> str | None:
    flux = kust.parent / "flux-kustomization.yaml"
    if not flux.exists():
        return None
    for doc in yaml.safe_load_all(flux.read_text()):
        if isinstance(doc, dict) and (ns := doc.get("spec", {}).get("targetNamespace")):
            return str(ns)
    return None


def test_generators_declare_a_namespace(k8s_dir: Path) -> None:
    offenders: list[str] = []
    for kust in k8s_dir.rglob("kustomization.yaml"):
        doc = yaml.safe_load(kust.read_text())
        if not isinstance(doc, dict):
            continue
        # Only directories Flux applies directly are checked. A base or overlay
        # fragment has no flux-kustomization.yaml and inherits its namespace from
        # whichever parent includes it, so requiring one there would be wrong --
        # e.g. grocy/user-perms-base and seaweedfs/cluster.
        if not (kust.parent / "flux-kustomization.yaml").exists():
            continue
        # A namespace supplied for the whole kustomization, by either mechanism,
        # covers every generated resource in it.
        if doc.get("namespace") or _flux_target_namespace(kust):
            continue
        # A generator used purely as a `replacements` source is never applied, so
        # it needs no namespace -- e.g. seaweedfs/cluster's filer.toml, which is
        # spliced into a Helm value and confirmed absent from the live cluster.
        replacement_sources = {r.get("source", {}).get("name") for r in doc.get("replacements") or []}
        for field in GENERATOR_FIELDS:
            for entry in doc.get(field) or []:
                if entry.get("name") in replacement_sources:
                    continue
                if not entry.get("namespace"):
                    offenders.append(
                        f"{kust.relative_to(k8s_dir)}: {field} entry "
                        f"{entry.get('name', '<unnamed>')!r} has no `namespace:`"
                    )

    assert not offenders, (
        "Generated ConfigMaps/Secrets have no namespace of their own, so the apply fails with "
        '"namespace not specified" and Flux applies nothing from that directory. '
        "Add `namespace: <ns>` to the kustomization:\n" + "\n".join(f"  {o}" for o in offenders)
    )


if __name__ == "__main__":
    pytest_bazel.main()
