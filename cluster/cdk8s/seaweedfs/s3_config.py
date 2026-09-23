"""The s3 gateway's `seaweedfs-s3-config` Secret, assembled by ESO from the per-tenant
`s3-identity-*-json` Secrets, the `seaweedfs-identities` SecretStore and the ServiceAccount it
reads them as.

Hand-written beside the output: the per-tenant `identities/*.sops.yaml`.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecDataFrom,
    ExternalSecretSpecDataFromFind,
    ExternalSecretSpecDataFromFindName,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetTemplate,
)
from external_secrets_secretstore_crds.io.external_secrets import (
    SecretStore,
    SecretStoreSpec,
    SecretStoreSpecProvider,
    SecretStoreSpecProviderKubernetes,
    SecretStoreSpecProviderKubernetesAuth,
    SecretStoreSpecProviderKubernetesAuthServiceAccount,
    SecretStoreSpecProviderKubernetesServer,
    SecretStoreSpecProviderKubernetesServerCaProvider,
    SecretStoreSpecProviderKubernetesServerCaProviderType,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.external_secrets.external_secret import add_external_secret, secret_store
from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.seaweedfs import namespace

SECRET_NAME = "seaweedfs-s3-config"
SECRET_KEY = "seaweedfs_s3_config.json"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/seaweedfs/secrets"
_CHART = "s3-config"
_READER = "eso-reader"
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
    # In-namespace SecretStore that the global + per-tenant ExternalSecrets read from. Provider =
    # kubernetes (looks back at the same cluster).
    SecretStore(
        chart,
        "secret-store",
        metadata=metadata(
            _SECRET_STORE,
            namespace.NAME,
            annotations={
                "description": (
                    "Reads per-tenant s3-identity-* Secrets and the per-tenant intermediate Secrets that the "
                    "assembly ExternalSecrets produce."
                )
            },
        ),
        spec=SecretStoreSpec(
            provider=SecretStoreSpecProvider(
                kubernetes=SecretStoreSpecProviderKubernetes(
                    server=SecretStoreSpecProviderKubernetesServer(
                        ca_provider=SecretStoreSpecProviderKubernetesServerCaProvider(
                            type=SecretStoreSpecProviderKubernetesServerCaProviderType.CONFIG_MAP,
                            name="kube-root-ca.crt",
                            key="ca.crt",
                        )
                    ),
                    auth=SecretStoreSpecProviderKubernetesAuth(
                        service_account=SecretStoreSpecProviderKubernetesAuthServiceAccount(
                            name=_READER, namespace=namespace.NAME
                        )
                    ),
                    remote_namespace=namespace.NAME,
                )
            )
        ),
    )
    # Concatenates the per-tenant intermediate Secrets' pre-rendered identity JSON snippets
    # into the blob the s3 gateway consumes. `creationPolicy: Owner` so ESO creates the
    # Secret when it is missing; `Merge` only updates an existing one.
    #
    # Template note: ESO's `dataFrom.find` returns `.<secretName>` as a JSON-encoded *string*
    # of that Secret's data dict, not a Go map. Each per-tenant intermediate Secret has data
    # key `identity` holding the identity JSON, hence `| fromJson` and then `.identity`.
    add_external_secret(
        chart,
        "s3-config",
        name=SECRET_NAME,
        namespace=namespace.NAME,
        refresh="1m",
        store=secret_store(_SECRET_STORE),
        data_from=[
            ExternalSecretSpecDataFrom(
                find=ExternalSecretSpecDataFromFind(
                    name=ExternalSecretSpecDataFromFindName(regexp="^s3-identity-.+-json$")
                )
            )
        ],
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
        annotations={"description": "Assembles the s3 gateway config Secret from per-tenant identity Secrets."},
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        # Per-tenant credentials. Legacy entries also contain an ExternalSecret that renders static
        # gateway JSON. Migrated entries contain only the SOPS Secret; their S3Identity/S3Credentials
        # CRs live with the corresponding Bucket CR.
        kustomize_kustomization(resources=[f"{_CHART}.k8s.yaml", "identities/admin.sops.yaml"]),
    )


def seaweedfs_secrets(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    seaweedfs_namespace: Kustomization,
    external_secrets_operator: Kustomization,
) -> Kustomization:
    name = "seaweedfs-secrets"
    return flux_kustomization(
        chart,
        name,
        artifact,
        retry_interval=None,
        wait=None,
        suspend=False,
        decryption=SOPS_DECRYPTION,
        depends_on=flux_kustomization_depends_on_many(
            seaweedfs_namespace,
            # ExternalSecret + SecretStore CRDs + ESO controller
            external_secrets_operator,
        ),
    )
