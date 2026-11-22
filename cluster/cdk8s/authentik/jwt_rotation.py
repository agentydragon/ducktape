"""The authentik-jwt-rotation CronJob (cluster/generated/agents/authentik-jwt-rotation), the
agents-infra namespace it owns, the credentials it mounts, the Role letting it read back what
it publishes into flux-system, and the roster it runs. Source:
cluster/rotators/authentik_jwt_rotation.

`ROTATIONS` is the rotator's own `Config`, rendered into the hash-suffixed ConfigMap the job
mounts. It only names each output: the rotator alone writes the SOPS files and the published
Secret manifests. The image tag is the placeholder "unset"; the hand-written `PINS_DIR`
Component, which the kustomization includes across the roots, overrides it at
`kustomize build` time via Flux's image-automation marker (cluster/cdk8s/AGENTS.md § the
`:tag` Setters marker).
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks

from cluster.cdk8s import external_creds, namespaces
from cluster.cdk8s.authentik import jwt_roster
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.flux import (
    ConfigMapArgs,
    Kustomization,
    RenderedDirectory,
    flux_kustomization,
    flux_kustomization_depends_on,
)
from cluster.cdk8s.forgejo_registry.pull_credentials import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, remote_data
from cluster.cdk8s.rotator_cron_job import GITHUB_PAT_SECRET_NAME, RotatorCronJob, secret_mount

NAME = "authentik-jwt-rotation"
OUTPUT_DIR = f"{GENERATED_ROOT}/agents/authentik-jwt-rotation"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/agents/authentik-jwt-rotation-image-pins"
NAMESPACE = "agents-infra"
_IMAGE = "git.allegedly.works/ducktape-ci/authentik-jwt-rotation:unset"
_PUBLISHED_SECRETS_READER = "authentik-jwt-rotation-published-secrets-reader"
_CONFIG_FILE = "rotations.yaml"


# The content hash in the ConfigMap's name rolls the CronJob's template on a roster change.
CONFIG_MAP = ConfigMapArgs(
    name="authentik-jwt-rotations-config", namespace=NAMESPACE, data={_CONFIG_FILE: yaml_config(jwt_roster.ROTATIONS)}
)


def _cronjob(chart: Chart) -> None:
    RotatorCronJob(
        chart,
        "cronjob",
        metadata=k8s.ObjectMeta(
            name=NAME,
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Rotates every Authentik client_credentials JWT listed in rotations.yaml (the"
                    " ConfigMap-mounted source of truth). Runs hourly; rotate.py reads each output's"
                    " unencrypted `expires_unencrypted` field (no decryption needed) and skips entries"
                    " with > rotate_below_hours remaining, so a real mint runs only ~every 44 days per"
                    " token. Entries with a `probe` also verify the published in-cluster Secret's token"
                    " against its real endpoint each run and re-mint on 401/403. Everything minted this"
                    " cycle lands in one commit."
                )
            },
        ),
        schedule="15 * * * *",
        image=_IMAGE,
        config_map_name=CONFIG_MAP.name,
        config_file=_CONFIG_FILE,
        # The SA token is used only to read back the published Secrets for probes
        # (the published-secrets-reader Roles, get on the rotator-published names).
        service_account_name=NAME,
        automount_service_account_token=True,
        image_pull_secrets=None,
        credential_mounts=tuple(
            secret_mount(c.volume, c.secret, str(c.directory)) for c in jwt_roster.client_credentials()
        ),
    )


def _published_secrets_reader(chart: Chart) -> None:
    k8s.KubeRole(
        chart,
        "published-secrets-reader-role",
        metadata=k8s.ObjectMeta(
            name=_PUBLISHED_SECRETS_READER,
            namespace=jwt_roster.PUBLISHED_SECRET_NAMESPACE,
            annotations={
                "description": (
                    "Lets the authentik-jwt-rotation CronJob read back exactly the Secrets its"
                    " k8s_secret outputs publish, so probes can verify the live tokens against"
                    " their real endpoints. resourceNames must stay in sync with the k8s_secret"
                    " names in rotations.yaml."
                )
            },
        ),
        rules=[
            k8s.PolicyRule(
                api_groups=[""],
                resources=["secrets"],
                verbs=["get"],
                resource_names=[
                    rotation.k8s_secret.name
                    for rotation in jwt_roster.ROTATIONS.rotations
                    if rotation.k8s_secret and rotation.k8s_secret.namespace == jwt_roster.PUBLISHED_SECRET_NAMESPACE
                ],
            )
        ],
    )
    k8s.KubeRoleBinding(
        chart,
        "published-secrets-reader-rolebinding",
        metadata=k8s.ObjectMeta(name=_PUBLISHED_SECRETS_READER, namespace=jwt_roster.PUBLISHED_SECRET_NAMESPACE),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=_PUBLISHED_SECRETS_READER),
        subjects=[k8s.Subject(kind="ServiceAccount", name=NAME, namespace=NAMESPACE)],
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=NAMESPACE)
    namespaces.namespace(chart, "namespace", name=NAMESPACE, vpa=Vpa.RECOMMEND, labels={"name": NAMESPACE})
    k8s.KubeServiceAccount(
        chart, "external-creds-reader", metadata=k8s.ObjectMeta(name="external-creds-reader", namespace=NAMESPACE)
    )
    ExternalSecret(
        chart,
        "github-pat",
        metadata=ApiObjectMetadata(name=GITHUB_PAT_SECRET_NAME, namespace=NAMESPACE),
        refresh_interval="1h",
        secret_store_ref=external_creds.STORE,
        data=[remote_data("github-agentydragon-2", "token")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
        template=ExternalSecretSpecTargetTemplate(type="Opaque"),
    )
    k8s.KubeServiceAccount(
        chart,
        "serviceaccount",
        metadata=k8s.ObjectMeta(
            name=NAME,
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Identity for the authentik-jwt-rotation CronJob. Exists solely so the"
                    " published-secrets-reader RoleBinding in flux-system can let the job read"
                    " back the Secrets its own k8s_secret outputs publish (probe support)."
                )
            },
        ),
        image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
    )
    _published_secrets_reader(chart)
    _cronjob(chart)
    return chart


def authentik_jwt_rotation(
    chart: Chart,
    directory: RenderedDirectory,
    external_secrets_operator: Kustomization,
    cert_manager_environment: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        wait=None,
        depends_on=[
            flux_kustomization_depends_on(external_secrets_operator),
            flux_kustomization_depends_on(cert_manager_environment),
        ],
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="external-secrets.io/v1",
                kind="ExternalSecret",
                name=GITHUB_PAT_SECRET_NAME,
                namespace=NAMESPACE,
            )
        ],
        timeout="2m",
    )
