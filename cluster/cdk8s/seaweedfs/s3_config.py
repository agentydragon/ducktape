"""The s3 gateway's `seaweedfs-s3-config` Secret, assembled by ESO from the per-tenant
`s3-identity-*-json` Secrets, and the ServiceAccount the `seaweedfs-identities` SecretStore
reads them as.

Hand-written beside the output: `secretstore.yaml` (no namespaced `SecretStore` binding
yet), the per-tenant `identities/*.sops.yaml` and the directory's `kustomization.yaml`.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import (
    ExternalSecret,
    ExternalSecretSpec,
    ExternalSecretSpecDataFrom,
    ExternalSecretSpecDataFromFind,
    ExternalSecretSpecDataFromFindName,
    ExternalSecretSpecSecretStoreRef,
    ExternalSecretSpecSecretStoreRefKind,
    ExternalSecretSpecTarget,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetTemplate,
)

from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.seaweedfs import namespace

SECRET_NAME = "seaweedfs-s3-config"
SECRET_KEY = "seaweedfs_s3_config.json"
OUTPUT_DIR = "cluster/k8s/seaweedfs/secrets"
_CHART = "s3-config"
_READER = "eso-reader"
# Hand-written in secretstore.yaml.
_SECRET_STORE = "seaweedfs-identities"


def chart(app: App) -> Chart:
    chart = Chart(app, _CHART, disable_resource_name_hashes=True)
    # The SecretStore's identity when reading source Secrets from this namespace.
    k8s.KubeServiceAccount(chart, "reader", metadata=k8s.ObjectMeta(name=_READER, namespace=namespace.NAME))
    k8s.KubeRole(
        chart,
        "reader-role",
        metadata=k8s.ObjectMeta(name=_READER, namespace=namespace.NAME),
        rules=[k8s.PolicyRule(api_groups=[""], resources=["secrets"], verbs=["get", "list", "watch"])],
    )
    k8s.KubeRoleBinding(
        chart,
        "reader-binding",
        metadata=k8s.ObjectMeta(name=_READER, namespace=namespace.NAME),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=_READER),
        subjects=[k8s.Subject(kind="ServiceAccount", name=_READER, namespace=namespace.NAME)],
    )
    # Concatenates the per-tenant intermediate Secrets' pre-rendered identity JSON snippets
    # into the blob the s3 gateway consumes. `creationPolicy: Owner` so ESO creates the
    # Secret when it is missing; `Merge` only updates an existing one.
    #
    # Template note: ESO's `dataFrom.find` returns `.<secretName>` as a JSON-encoded *string*
    # of that Secret's data dict, not a Go map. Each per-tenant intermediate Secret has data
    # key `identity` holding the identity JSON, hence `| fromJson` and then `.identity`.
    ExternalSecret(
        chart,
        "s3-config",
        metadata=metadata(
            SECRET_NAME,
            namespace.NAME,
            annotations={"description": "Assembles the s3 gateway config Secret from per-tenant identity Secrets."},
        ),
        spec=ExternalSecretSpec(
            refresh_interval="1m",
            secret_store_ref=ExternalSecretSpecSecretStoreRef(
                kind=ExternalSecretSpecSecretStoreRefKind.SECRET_STORE, name=_SECRET_STORE
            ),
            target=ExternalSecretSpecTarget(
                name=SECRET_NAME,
                creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
                template=ExternalSecretSpecTargetTemplate(
                    type="Opaque",
                    data={
                        SECRET_KEY: (
                            '{"identities":[\n'
                            "  {{- $first := true -}}\n"
                            "  {{- range $sName, $sJson := . -}}\n"
                            "  {{- $sData := $sJson | fromJson -}}\n"
                            "  {{- if not $first }},{{ end -}}{{ $sData.identity }}{{- $first = false -}}\n"
                            "  {{- end -}}\n"
                            "]}\n"
                        )
                    },
                ),
            ),
            data_from=[
                ExternalSecretSpecDataFrom(
                    find=ExternalSecretSpecDataFromFind(
                        name=ExternalSecretSpecDataFromFindName(regexp="^s3-identity-.+-json$")
                    )
                )
            ],
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
