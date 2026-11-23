"""The Authentik JWT rotation roster and the client credentials its entries mount."""

from pathlib import Path

from cluster.cdk8s.haku import mailbox_endpoint
from cluster.rotators.authentik_jwt_rotation.config import Config, K8sSecretOutput, Probe, Rotation

PUBLISHED_SECRET_NAMESPACE = "flux-system"

_CREDENTIALS_ROOT = Path("/var/run/secrets/authentik")
_KUBECTL_SANDBOX = _CREDENTIALS_ROOT / "claude-web-k8s"
_HAKU = _CREDENTIALS_ROOT / "haku-k8s"
_HAKU_MAIL = _CREDENTIALS_ROOT / "haku-mail"
_ALLOY_OTLP = _CREDENTIALS_ROOT / "alloy-otlp"

# Source Secrets provisioned by tf/gitops/agent-machine-access in agents-infra.
_SECRET_BY_DIRECTORY = {
    _KUBECTL_SANDBOX: "kubectl-sandbox-client-credentials",
    _HAKU: "haku-client-credentials",
    _HAKU_MAIL: "haku-mail-client-credentials",
    _ALLOY_OTLP: "alloy-otlp-client-credentials",
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
            credentials_dir=_KUBECTL_SANDBOX,
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
            credentials_dir=_HAKU,
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
            credentials_dir=_HAKU_MAIL,
            sops_file=Path("secrets/haku-mail-jwt.yaml"),
            token_field="jwt",
            # Publish as an in-cluster Secret; the haku-mail-token ClusterExternalSecret
            # (cluster/k8s/haku/mailbox) mirrors it into haku-sandbox for Haku.
            k8s_secret=K8sSecretOutput(
                path=Path("cluster/k8s/haku/mailbox/haku-mail-token.sops.yaml"),
                name="haku-mail-token",
                namespace=PUBLISHED_SECRET_NAMESPACE,
            ),
            # Stalwart 401s a bad bearer and 403s a token whose email claim can't be resolved to
            # a mailbox — both are credential verdicts worth a re-mint (the re-mint then asserts
            # expected_claims loudly).
            probe=Probe(url=f"{mailbox_endpoint.PUBLIC_URL}/jmap/session"),
        ),
        Rotation(
            name="alloy-otlp",
            provider_slug="alloy-otlp-client-credentials",
            scopes="openid profile email",
            exchange_scopes="openid profile email ak_proxy",
            credentials_dir=_ALLOY_OTLP,
            sops_file=Path("secrets/alloy-otlp-bearer-token.yaml"),
            token_field="token",
            # Also publish in-cluster; the alloy-otlp-bearer ClusterExternalSecret
            # (cluster/k8s/agents/alloy-otlp-bearer) mirrors it into the agent sandbox namespaces
            # for the session OTLP forwarder (devinfra/claude/otlp_forwarder.py).
            k8s_secret=K8sSecretOutput(
                path=Path("cluster/k8s/agents/alloy-otlp-bearer/alloy-otlp-bearer.sops.yaml"),
                name="alloy-otlp-bearer",
                namespace=PUBLISHED_SECRET_NAMESPACE,
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


def client_credentials() -> dict[Path, str]:
    """Map each directory used by a rotation to the Secret the CronJob mounts there."""
    return {
        rotation.credentials_dir: _SECRET_BY_DIRECTORY[rotation.credentials_dir] for rotation in ROTATIONS.rotations
    }
