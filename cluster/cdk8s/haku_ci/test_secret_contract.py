"""The authored Forgejo token Secret satisfies haku-ci's KEDA source contract."""

from __future__ import annotations

import yaml
from python.runfiles import Runfiles


def test_keda_auth_reads_the_forgejo_token_secret() -> None:
    root = Runfiles.CreateOrRaise().root() / "ducktape"
    (authentication,) = (
        resource
        for resource in yaml.safe_load_all((root / "cluster/generated/haku-ci/haku-ci.k8s.yaml").read_text())
        if resource["kind"] == "TriggerAuthentication"
    )
    token = yaml.safe_load((root / "cluster/k8s/haku/forgejo-tea/haku-forgejo-tea.sops.yaml").read_text())

    (source,) = authentication["spec"]["secretTargetRef"]
    assert source["parameter"] == "token"
    assert token["metadata"]["name"] == source["name"]
    assert source["key"] in token["stringData"]
    # Reflector copies the Secret from haku-sandbox into the namespace KEDA reads it in.
    annotations = token["metadata"]["annotations"]
    assert (
        annotations["reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces"]
        == authentication["metadata"]["namespace"]
    )
    assert (
        annotations["reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"]
        == authentication["metadata"]["namespace"]
    )
