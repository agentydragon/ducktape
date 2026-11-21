"""The authentik-jwt-rotation CronJob (cluster/generated/agents/authentik-jwt-rotation), the
agents-infra namespace it owns, the credentials it mounts, the Role letting it read back what
it publishes into flux-system, and `ROTATIONS`, the roster it runs. Source:
cluster/rotators/authentik_jwt_rotation.

`ROTATIONS` is the rotator's own `Config`, rendered into the hash-suffixed ConfigMap the job
mounts. It only names each output: the rotator alone writes the SOPS files and the published
Secret manifests. The image tag is the placeholder "unset"; the hand-written `PINS_DIR`
Component, which the kustomization includes across the roots, overrides it at
`kustomize build` time via Flux's image-automation marker (cluster/cdk8s/AGENTS.md § the
`:tag` Setters marker).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks

from cluster.cdk8s import external_creds, namespaces
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.flux import (
    ConfigMapArgs,
    Kustomization,
    RenderedDirectory,
    flux_kustomization,
    flux_kustomization_depends_on,
)
from cluster.cdk8s.forgejo_registry.pull_credentials import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.haku import mailbox
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, remote_data
from cluster.cdk8s.rotator_cron_job import GITHUB_PAT_SECRET_NAME, RotatorCronJob, secret_mount
from cluster.rotators.authentik_jwt_rotation.config import Config, K8sSecretOutput, Probe, Rotation

NAME = "authentik-jwt-rotation"
OUTPUT_DIR = f"{GENERATED_ROOT}/agents/authentik-jwt-rotation"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/agents/authentik-jwt-rotation-image-pins"
NAMESPACE = "agents-infra"
_IMAGE = "git.allegedly.works/ducktape-ci/authentik-jwt-rotation:unset"
_PUBLISHED_SECRETS_READER = "authentik-jwt-rotation-published-secrets-reader"
_FLUX_SYSTEM = "flux-system"
_CONFIG_FILE = "rotations.yaml"


@dataclass(frozen=True)
class _ClientCredentials:
    """A client-credentials Secret tf/gitops/agent-machine-access writes into agents-infra, and the
    directory the job mounts it at: the `credentials_dir` of the rotations authenticating with it."""

    name: str
    secret: str

    @property
    def volume(self) -> str:
        return f"{self.name}-creds"

    @property
    def directory(self) -> Path:
        return Path("/var/run/secrets/authentik") / self.name


_KUBECTL_SANDBOX = _ClientCredentials(name="claude-web-k8s", secret="kubectl-sandbox-client-credentials")
_HAKU = _ClientCredentials(name="haku-k8s", secret="haku-client-credentials")
_HAKU_MAIL = _ClientCredentials(name="haku-mail", secret="haku-mail-client-credentials")
_ALLOY_OTLP = _ClientCredentials(name="alloy-otlp", secret="alloy-otlp-client-credentials")
_CLIENT_CREDENTIALS_BY_DIRECTORY = {
    credential.directory: credential for credential in (_KUBECTL_SANDBOX, _HAKU, _HAKU_MAIL, _ALLOY_OTLP)
}

# Each entry mints (and for proxy consumers, exchanges) an Authentik client_credentials JWT and
# commits it SOPS-encrypted.
ROTATIONS = Config(
    rotations=[
        Rotation(
            name="claude-web-k8s",
            provider_slug="kubectl-sandbox-client-credentials",
            scopes="openid profile email groups",
            expected_group="kubectl-sandbox-users",
            credentials_dir=_KUBECTL_SANDBOX.directory,
            sops_file=Path("secrets/claude-web-k8s-jwt.yaml"),
            token_field="jwt",
        ),
        Rotation(
            name="haku-k8s",
            provider_slug="kubectl-sandbox-client-credentials",
            scopes="openid profile email groups",
            credential_mode="user_password",
            expected_group="haku",
            # Assert the provider-default audience (its client_id), which keeps the direct
            # kubeapi.allegedly.works path working.
            expected_audiences=["kubectl-sandbox-client-credentials"],
            credentials_dir=_HAKU.directory,
            sops_file=Path("secrets/haku-k8s-jwt.yaml"),
            token_field="jwt",
            # This JWT remains available to the CLI/write_kubeconfig path. The parked
            # cloud-agent vault no longer has a consumer for a Flux-published copy.
        ),
        Rotation(
            # JWT for haku's mailbox (Stalwart, cluster/k8s/haku/mailbox). The server's OIDC
            # directory validates tokens against the dedicated stalwart-haku provider and pins
            # requireAudience to its client_id; haku presents this as an HTTP bearer on JMAP.
            # Same `haku` SA/app-password as the shared service account
            # (tf/gitops/agent-machine-access/haku-service-account.tf); the provider determines
            # the audience.
            name="haku-mail",
            provider_slug="stalwart-haku",
            scopes="openid profile email",
            credential_mode="user_password",
            expected_audiences=["stalwart-haku"],
            # Stalwart's OIDC directory requires a non-empty "email" claim to resolve a JWT to a
            # mailbox account (requireScopes.email); an empty claim 403s at /jmap/session even
            # though the token authenticates fine. Asserting it here fails the mint loudly
            # instead of silently shipping a broken token, and forces a re-mint if the stamped
            # claim ever stops matching (e.g. if the Authentik user's email attribute is unset
            # again).
            expected_claims={"email": "haku@allegedly.works"},
            credentials_dir=_HAKU_MAIL.directory,
            sops_file=Path("secrets/haku-mail-jwt.yaml"),
            token_field="jwt",
            # Publish as an in-cluster Secret; the haku-mail-token ClusterExternalSecret
            # (cluster/k8s/haku/mailbox) mirrors it into haku-sandbox for Haku.
            k8s_secret=K8sSecretOutput(
                path=Path("cluster/k8s/haku/mailbox/haku-mail-token.sops.yaml"),
                name="haku-mail-token",
                namespace=_FLUX_SYSTEM,
            ),
            # Stalwart 401s a bad bearer and 403s a token whose email claim can't be resolved to
            # a mailbox — both are credential verdicts worth a re-mint (the re-mint then asserts
            # expected_claims loudly).
            probe=Probe(url=f"{mailbox.PUBLIC_URL}/jmap/session"),
        ),
        Rotation(
            name="alloy-otlp",
            provider_slug="alloy-otlp-client-credentials",
            scopes="openid profile email",
            exchange_scopes="openid profile email ak_proxy",
            credentials_dir=_ALLOY_OTLP.directory,
            sops_file=Path("secrets/alloy-otlp-bearer-token.yaml"),
            token_field="token",
            # Also publish in-cluster; the alloy-otlp-bearer ClusterExternalSecret
            # (cluster/k8s/agents/alloy-otlp-bearer) mirrors it into the agent sandbox namespaces
            # for the session OTLP forwarder (devinfra/claude/otlp_forwarder.py).
            k8s_secret=K8sSecretOutput(
                path=Path("cluster/k8s/agents/alloy-otlp-bearer/alloy-otlp-bearer.sops.yaml"),
                name="alloy-otlp-bearer",
                namespace=_FLUX_SYSTEM,
                # The proxy-scoped bearer is not a k8s-auth JWT; keep the key aligned with the
                # ClusterExternalSecret's remoteRef.property and the forwarder's jsonpath (both
                # read `token`). Without this the default (`jwt`) would silently break the mirror
                # on the first automated mint.
                token_key="token",
            ),
            # The Authentik outpost 401s a bad bearer before Alloy sees the request; an empty
            # POST past auth gets a non-auth status from Alloy, which is fine.
            probe=Probe(url="https://alloy-otlp.allegedly.works/v1/metrics", method="POST"),
        ),
    ]
)

# The content hash in the ConfigMap's name rolls the CronJob's template on a roster change.
CONFIG_MAP = ConfigMapArgs(
    name="authentik-jwt-rotations-config", namespace=NAMESPACE, data={_CONFIG_FILE: yaml_config(ROTATIONS)}
)


def _rotation_client_credentials() -> tuple[_ClientCredentials, ...]:
    """Return the distinct credential Secrets used by ROTATIONS, in roster order."""
    credentials: dict[Path, _ClientCredentials] = {}
    for rotation in ROTATIONS.rotations:
        credential = _CLIENT_CREDENTIALS_BY_DIRECTORY.get(rotation.credentials_dir)
        if credential is None:
            raise ValueError(f"no Kubernetes client-credential Secret is mapped for {rotation.credentials_dir}")
        credentials.setdefault(rotation.credentials_dir, credential)
    return tuple(credentials.values())


def _cronjob(chart: Chart) -> None:
    client_credentials = _rotation_client_credentials()
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
        credential_mounts=tuple(secret_mount(c.volume, c.secret, str(c.directory)) for c in client_credentials),
    )


def _published_secrets_reader(chart: Chart) -> None:
    k8s.KubeRole(
        chart,
        "published-secrets-reader-role",
        metadata=k8s.ObjectMeta(
            name=_PUBLISHED_SECRETS_READER,
            namespace=_FLUX_SYSTEM,
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
                    for rotation in ROTATIONS.rotations
                    if rotation.k8s_secret and rotation.k8s_secret.namespace == _FLUX_SYSTEM
                ],
            )
        ],
    )
    k8s.KubeRoleBinding(
        chart,
        "published-secrets-reader-rolebinding",
        metadata=k8s.ObjectMeta(name=_PUBLISHED_SECRETS_READER, namespace=_FLUX_SYSTEM),
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
