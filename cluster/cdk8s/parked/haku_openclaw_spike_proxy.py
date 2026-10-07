"""Retired OpenClaw spike's Iron proxy. Rendered only under cluster/parked for revival."""

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)

from cluster.cdk8s import egress_fences, external_creds
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.forgejo_images import SECRET_NAME
from cluster.cdk8s.manifest_roots import PARKED_ROOT
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, remote_data
from cluster.cdk8s.secret_ref import SecretKey, SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "haku-egress-proxy"
OUTPUT_DIR = f"{PARKED_ROOT}/haku-openclaw-spike/proxy"
_CA_SECRET = "haku-egress-proxy-ca"
_IRON_PROXY_IMAGE = "git.allegedly.works/ducktape-ci/iron-proxy:unset"
_IRON_PROXY_METRICS = Port(name="metrics", number=9090)
# The OpenClaw spike's only egress path.
OPENCLAW_SPIKE_PROXY = ServiceRef(
    name="haku-openclaw-spike-proxy",
    port=Port(name="proxy", number=8181),
    pods=Pods(namespace=NAME, labels=(("app.kubernetes.io/name", "haku-openclaw-spike-proxy"),)),
)
_PUBLISHED_SECRETS_READER = "authentik-jwt-rotation-published-secrets-reader"


def _quantities(**values: str) -> dict[str, k8s.Quantity]:
    return {key: k8s.Quantity.from_string(value) for key, value in values.items()}


def _iron_proxy(chart: Chart, service: ServiceRef, *, description: str, config: dict, env: list[k8s.EnvVar]) -> None:
    """An iron-proxy Deployment holding real credentials and substituting them for a sandbox's
    placeholders, its config, and its Service."""
    name = service.name
    labels = service.pods.selector
    # No content-hash name suffix: Reloader's `autoReloadAll` rolls the proxy when this changes.
    config_map = k8s.KubeConfigMap(
        chart,
        f"{name}-config",
        metadata=k8s.ObjectMeta(name=f"{name}-config", namespace=NAME),
        data={"iron.yaml": yaml_config(config)},
    )
    k8s.KubeDeployment(
        chart,
        f"{name}-deployment",
        metadata=k8s.ObjectMeta(name=name, namespace=NAME, labels=labels, annotations={"description": description}),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=labels),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=labels),
                spec=k8s.PodSpec(
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    automount_service_account_token=False,
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True,
                        run_as_user=65532,
                        run_as_group=65532,
                        seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                    ),
                    containers=[
                        k8s.Container(
                            name="iron-proxy",
                            image=_IRON_PROXY_IMAGE,
                            args=["-config", "/etc/iron-proxy/iron.yaml"],
                            env=env,
                            ports=[service.port.k8s_container_port(), _IRON_PROXY_METRICS.k8s_container_port()],
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False, capabilities=k8s.Capabilities(drop=["ALL"])
                            ),
                            resources=k8s.ResourceRequirements(
                                requests=_quantities(cpu="50m", memory="128Mi"),
                                limits=_quantities(cpu="500m", memory="512Mi"),
                            ),
                            volume_mounts=[
                                k8s.VolumeMount(name="config", mount_path="/etc/iron-proxy", read_only=True),
                                k8s.VolumeMount(name="ca", mount_path="/ca", read_only=True),
                            ],
                        )
                    ],
                    volumes=[
                        k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name=config_map.name)),
                        k8s.Volume(name="ca", secret=k8s.SecretVolumeSource(secret_name=_CA_SECRET)),
                    ],
                ),
            ),
        ),
    )
    k8s.KubeService(
        chart,
        f"{name}-service",
        metadata=k8s.ObjectMeta(name=name, namespace=NAME),
        spec=k8s.ServiceSpec(
            selector=labels, ports=[service.port.k8s_service_port(), _IRON_PROXY_METRICS.k8s_service_port()]
        ),
    )


def _github_token(chart: Chart, token: SecretKey) -> None:
    """The agentydragon-agent GitHub PAT, consumed only by one iron-proxy here; its sandbox
    receives a non-secret placeholder that the proxy replaces in Authorization headers for
    exact GitHub hosts."""
    ExternalSecret(
        chart,
        token.secret.name,
        metadata=ApiObjectMetadata(name=token.secret.name, namespace=token.secret.namespace),
        refresh_interval="1h",
        secret_store_ref=external_creds.STORE,
        data=[remote_data("github-agentydragon-agent", "token", secret_key=token.key)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
    )


def _substitution(env: str, placeholder: str, *hosts: str) -> dict:
    """An iron `secrets` entry: the value of `env` replaces `placeholder` in Authorization, on
    `hosts` only. iron-proxy also substitutes inside base64 Basic authorization, which is how
    git over HTTP carries a password."""
    return {
        "source": {"type": "env", "var": env},
        "replace": {"proxy_value": placeholder, "match_headers": ["Authorization"]},
        "rules": [{"host": host} for host in hosts],
    }


def _openclaw_spike_iron_config() -> dict:
    return {
        "dns": {"enabled": False},
        "proxy": {
            "tunnel_listen": f":{OPENCLAW_SPIKE_PROXY.pod_port}",
            # Forgejo generates a full-history Git pack before returning response headers. The
            # default 30s cap aborts that request with HTTP 502, while shallow fetches finish in
            # time. Keep a bounded but practical limit.
            "upstream_response_header_timeout": "5m",
        },
        "tls": {"mode": "mitm", "ca_cert": "/ca/tls.crt", "ca_key": "/ca/tls.key"},
        "transforms": [
            # The L7 bound; the same tuple is the DNS half of the proxy's Cilium fence.
            {"name": "allowlist", "config": {"domains": list(egress_fences.OPENCLAW_SPIKE_ALLOWLIST)}},
            {
                "name": "secrets",
                "config": {
                    "secrets": [
                        _substitution(
                            "CLAUDE_CODE_OAUTH_TOKEN",
                            "sk-ant-oat01-proxy-haku-openclaw-placeholder",
                            "api.anthropic.com",
                        ),
                        _substitution("HAKU_GIT_PASSWORD", "proxy-haku-forgejo-placeholder", "forgejo-http.forgejo"),
                        _substitution(
                            "GITHUB_TOKEN",
                            "proxy-github-placeholder",
                            "api.github.com",
                            "github.com",
                            "codeload.github.com",
                        ),
                        # An Authentik client_credentials JWT for the `haku` k8s group, minted and
                        # rotated by agents/authentik-jwt-rotation. The apiserver derives
                        # oidc-ksbx-groups:haku from it, so what this runtime may do in the
                        # cluster is RBAC on that group -- this proxy only delivers the bearer,
                        # it cannot tell `get pods` from `delete ns`.
                        _substitution("HAKU_KUBE_JWT", "proxy-haku-kube-placeholder", "kubeapi.allegedly.works"),
                    ]
                },
            },
        ],
        "log": {"level": "info"},
    }


def _openclaw_spike_proxy(chart: Chart) -> None:
    # Shared with public-coder-agent's copy of the same PAT.
    github_token = SecretRef(namespace=NAME, name="haku-openclaw-spike-github-token").key("GITHUB_TOKEN")
    kube_token = SecretRef(namespace=NAME, name="haku-openclaw-spike-kube-token")
    _github_token(chart, github_token)
    _iron_proxy(
        chart,
        OPENCLAW_SPIKE_PROXY,
        description=(
            "Holds Haku OpenClaw spike credentials and substitutes placeholders only for exact destination hosts."
        ),
        config=_openclaw_spike_iron_config(),
        env=[
            SecretRef(namespace=NAME, name="haku-claude-oauth-token")
            .key("CLAUDE_CODE_OAUTH_TOKEN")
            .env_var("CLAUDE_CODE_OAUTH_TOKEN"),
            SecretRef(namespace=NAME, name="haku-forgejo-git").key("password").env_var("HAKU_GIT_PASSWORD"),
            github_token.env_var("GITHUB_TOKEN"),
            # Rotated roughly every 44 days by agents/authentik-jwt-rotation, which commits the
            # Secret SOPS-encrypted straight into THIS namespace -- deliberately not via the
            # shared claude-sandbox store that carries GITHUB_TOKEN above. That store is
            # conditioned to four namespaces including public-coder-agent, the one deliberately
            # unconfined fence in the cluster; a cluster-API bearer has exactly one consumer and
            # should be readable by exactly one namespace. Reloader's `autoReloadAll` restarts
            # this pod when Flux applies a rotation -- without it, kubectl would start 401ing
            # ~44 days after it last worked with nothing visibly changed.
            # Optional: this proxy is the ONLY egress path for the spike, so a missing kube
            # token must not take out Anthropic, Forgejo and GitHub too. Unset simply means no
            # substitution: kubectl 401s, everything else is untouched.
            kube_token.key("jwt").env_var("HAKU_KUBE_JWT", optional=True),
        ],
    )
    k8s.KubeNetworkPolicy(
        chart,
        "openclaw-spike-networkpolicy",
        metadata=k8s.ObjectMeta(name="allow-haku-openclaw-spike-proxy-ingress", namespace=NAME),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels=OPENCLAW_SPIKE_PROXY.pods.selector),
            policy_types=["Ingress"],
            ingress=[
                k8s.NetworkPolicyIngressRule(
                    from_=[
                        k8s.NetworkPolicyPeer(
                            namespace_selector=k8s.LabelSelector(
                                match_labels={"kubernetes.io/metadata.name": "haku-openclaw-spike"}
                            )
                        )
                    ],
                    ports=[
                        k8s.NetworkPolicyPort(
                            port=k8s.IntOrString.from_number(OPENCLAW_SPIKE_PROXY.pod_port), protocol="TCP"
                        )
                    ],
                )
            ],
        ),
    )
    # The authentik-jwt-rotation CronJob probes the tokens it publishes against their real
    # endpoints each run, which means reading back the published Secret. Its flux-system Role
    # does not cover this namespace, so it needs a read grant here, scoped to the one Secret
    # name. Keep in sync with the k8s_secret name in authentik_jwt_rotation.ROTATIONS.
    k8s.KubeRole(
        chart,
        "kube-token-probe-role",
        metadata=k8s.ObjectMeta(
            name=_PUBLISHED_SECRETS_READER,
            namespace=NAME,
            annotations={
                "description": (
                    "Lets the authentik-jwt-rotation CronJob read back the one Secret it"
                    " publishes here, so its probe can verify the live token against"
                    " kubeapi.allegedly.works. Nothing else in this namespace is readable."
                )
            },
        ),
        rules=[k8s.PolicyRule(api_groups=[""], resources=["secrets"], verbs=["get"], resource_names=[kube_token.name])],
    )
    k8s.KubeRoleBinding(
        chart,
        "kube-token-probe-rolebinding",
        metadata=k8s.ObjectMeta(name=_PUBLISHED_SECRETS_READER, namespace=NAME),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=_PUBLISHED_SECRETS_READER),
        subjects=[k8s.Subject(kind="ServiceAccount", name="authentik-jwt-rotation", namespace="agents-infra")],
    )


def chart(app: App) -> Chart:
    chart = Chart(app, "haku-openclaw-spike-proxy", disable_resource_name_hashes=True)
    k8s.KubeServiceAccount(
        chart, "external-creds-reader", metadata=k8s.ObjectMeta(name="external-creds-reader", namespace=NAME)
    )
    _openclaw_spike_proxy(chart)
    return chart
