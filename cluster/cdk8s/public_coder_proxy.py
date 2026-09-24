"""The public-coder-agent egress proxy, and the holder of its mediated credentials.

Deviation from agents/haku-zones-mitmproxy, which this used to mirror: it runs iron-proxy
rather than mitmproxy, because it now does two jobs. It is still the TLS-intercepting forward
proxy, and it additionally swaps a placeholder for the real credentials on the way out, so the
agent container does not hold them. mitmproxy could do that too, through an addon we wrote and
tested; iron-proxy was chosen because the substitution is a maintained project's product rather
than forty lines of ours, and because it handles the base64 `Basic` shape that git over HTTPS
uses, which our addon did not.

Destinations are deliberately unrestricted for this agent -- see `_egress_policy` for why, and
for the confined configuration kept ready to restore. What is not unrestricted is the
credential: iron.yaml scopes the substitution to GitHub hosts, so wider egress widens what data
can leave, not what the token can do.

Evidence for every choice here, including a real agent opening a pull request from behind this
configuration: docs/personal_agents/findings/egress_and_tls.md F15-F18.

The image tag is the placeholder "unset"; the hand-written
cluster/k8s/agents/public-coder-agent/proxy/image-pins/kustomization.yaml overrides it at
`kustomize build` time via Flux's image-automation marker. iron.yaml is the configMapGenerator
input of the hand-written kustomization.yaml.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from cert_manager_crds.io.cert_manager import (
    Certificate,
    CertificateSpec,
    CertificateSpecIssuerRef,
    CertificateSpecPrivateKey,
    CertificateSpecPrivateKeyAlgorithm,
    CertificateSpecSecretTemplate,
)
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)
from trust_manager_crds.io.cert_manager.trust import (
    Bundle,
    BundleSpec,
    BundleSpecSources,
    BundleSpecSourcesSecret,
    BundleSpecTarget,
    BundleSpecTargetConfigMap,
    BundleSpecTargetConfigMapMetadata,
    BundleSpecTargetNamespaceSelector,
    BundleSpecTargetNamespaceSelectorMatchExpressions,
)

from cluster.cdk8s import cilium, external_creds, public_coder_devbox
from cluster.cdk8s.clickhouse import client
from cluster.cdk8s.external_secrets.external_secret import add_external_secret, remote_data
from cluster.cdk8s.forgejo_images import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata

NAME = "public-coder-agent-proxy"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agents/public-coder-agent/proxy"
NAMESPACE = "public-coder-agent"
# The ConfigMap the kustomization.yaml's configMapGenerator renders from iron.yaml.
_CONFIG_MAP_NAME = "public-coder-agent-proxy-config"
_CA_SECRET_NAME = "public-coder-agent-proxy-ca"
LABELS = {"app.kubernetes.io/name": NAME}
_IMAGE = "git.allegedly.works/ducktape-ci/iron-proxy:unset"
PROXY_PORT = 8080
_METRICS_PORT = 9090


def _endpoint(namespace: str, labels: dict[str, str]) -> dict[str, str]:
    """Cilium's selector for Pods in `namespace` carrying `labels`, all as Kubernetes labels."""
    return {"k8s:io.kubernetes.pod.namespace": namespace, **{f"k8s:{key}": value for key, value in labels.items()}}


def _external_secrets(scope: Construct) -> None:
    forgejo_images_creds_external_secret(scope, "forgejo-images-creds", namespace=NAMESPACE)
    brave = "brave-search-api-key"
    add_external_secret(
        scope,
        "brave-search-api-key",
        name=brave,
        namespace=NAMESPACE,
        refresh="1m",
        store=external_creds.STORE,
        data=[remote_data(brave, "api-key")],
        # The existing target is Reflector-created. Orphan lets ESO sync it without
        # requiring an owner reference it does not currently have; the short interval
        # recreates the target promptly when the old reflected source is pruned.
        creation_policy=ExternalSecretSpecTargetCreationPolicy.ORPHAN,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
    )
    # Consumer-owned referent identity for source-approved external credentials.
    k8s.KubeServiceAccount(
        scope, "external-creds-reader", metadata=k8s.ObjectMeta(name="external-creds-reader", namespace=NAMESPACE)
    )


def _ca(scope: Construct) -> None:
    """A dedicated interception root, separate from the cluster internal CA, and the trust
    bundle publishing it. Standard TLS-interception trust pattern; see agents/mitmproxy/README.md
    for the rotation constraint (publish both roots in the Bundle before switching signing keys).

    ECDSA P-256 is deliberate and fine: iron-proxy accepts an ECDSA root and mints working leaves
    from it, verified against the pinned image.
    """
    root_ca = "public-coder-agent-proxy-root-ca"
    Certificate(
        scope,
        "root-ca",
        metadata=metadata(root_ca, NAMESPACE),
        spec=CertificateSpec(
            is_ca=True,
            common_name=root_ca,
            secret_name=_CA_SECRET_NAME,
            duration="87600h",  # 10 years
            renew_before="8760h",  # 1 year
            private_key=CertificateSpecPrivateKey(algorithm=CertificateSpecPrivateKeyAlgorithm.ECDSA, size=256),
            secret_template=CertificateSpecSecretTemplate(
                annotations={
                    "reflector.v1.k8s.emberstack.com/reflection-allowed": "true",
                    "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces": "cert-manager",
                    "reflector.v1.k8s.emberstack.com/reflection-auto-enabled": "true",
                    "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces": "cert-manager",
                }
            ),
            issuer_ref=CertificateSpecIssuerRef(name="cluster-ca-bootstrap", kind="ClusterIssuer"),
        ),
    )
    # Public roots + cluster root + this proxy's interception root, published into the agent
    # namespace so its TLS clients accept intercepted connections.
    Bundle(
        scope,
        "trust-bundle",
        metadata=ApiObjectMetadata(name="public-coder-agent-proxy-ca-cert"),
        spec=BundleSpec(
            sources=[
                BundleSpecSources(use_default_c_as=True),
                BundleSpecSources(secret=BundleSpecSourcesSecret(name="cluster-root-ca-secret", key="ca.crt")),
                BundleSpecSources(secret=BundleSpecSourcesSecret(name=_CA_SECRET_NAME, key="tls.crt")),
            ],
            target=BundleSpecTarget(
                config_map=BundleSpecTargetConfigMap(
                    key="ca-certificates.crt",
                    metadata=BundleSpecTargetConfigMapMetadata(
                        annotations={
                            "description": (
                                "Trust bundle for public-coder-agent HTTPS traffic intercepted by its egress proxy"
                            )
                        }
                    ),
                ),
                namespace_selector=BundleSpecTargetNamespaceSelector(
                    match_expressions=[
                        BundleSpecTargetNamespaceSelectorMatchExpressions(
                            key="kubernetes.io/metadata.name", operator="In", values=[NAMESPACE]
                        )
                    ]
                ),
            ),
        ),
    )


def _secret_env(name: str, secret_name: str, key: str) -> k8s.EnvVar:
    return k8s.EnvVar(
        name=name, value_from=k8s.EnvVarSource(secret_key_ref=k8s.SecretKeySelector(name=secret_name, key=key))
    )


def _container() -> k8s.Container:
    return k8s.Container(
        name="iron-proxy",
        # Bootstrap on upstream 0.49.0. Once CI publishes the commit-pinned Forgejo image, Flux
        # replaces this reference through the image-pins policy.
        # TODO(public-coder-agent): Return to the first official release containing
        # https://github.com/ironsh/iron-proxy/commit/c90f4fe31607552ed05675fc7ad239d94b431af2
        # and remove the temporary image build after verifying ALPN still negotiates h2.
        image=_IMAGE,
        args=["-config", "/etc/iron-proxy/iron.yaml"],
        env=[
            # The real GitHub credential lives here and nowhere else. It reaches the agent's
            # traffic only as a substitution performed here.
            _secret_env("GITHUB_TOKEN", "public-coder-agent-github-token", "GITHUB_TOKEN"),
            # Dedicated Haku Console bearer, held by the proxy rather than the agent. iron.yaml
            # substitutes it only for the exact public Haku host's Authorization header.
            _secret_env("HAKU_CONSOLE_TOKEN", "haku-console-public-coder-agent", "token"),
            # The agent sees only the corresponding placeholder. This password is valid solely for
            # the native read-only public_coder_analytics ClickHouse account and is substituted by
            # iron.yaml on the private ClusterIP host.
            _secret_env("CLICKHOUSE_PUBLIC_CODER_PASSWORD", client.PUBLIC_CODER_CREDENTIALS, client.PASSWORD_KEY),
            # The same bearer used by aiquota-api. It is reflected here solely for iron-proxy to
            # substitute into the agent's placeholder on the two read endpoints; the OpenClaw
            # workload never receives it.
            _secret_env("AIQUOTA_API_BEARER_TOKEN", "aiquota-api-bearer-public-coder", "bearer-token"),
            # The Brave Search API key is consumed only by iron-proxy. The OpenClaw Pod gets a
            # non-secret placeholder that is swapped only for Brave's X-Subscription-Token header
            # on its API host. Synced into this namespace from the external-creds source at
            # cluster/k8s/external-creds/brave-search-api-key.sops.yaml.
            _secret_env("BRAVE_API_KEY", "brave-search-api-key", "api-key"),
            # Matrix password login is the one credential that lives in a JSON body rather than
            # Authorization. iron.yaml swaps the app's placeholder only on the Matrix login
            # endpoint.
            _secret_env("MATRIX_BOT_PASSWORD", "public-coder-agent-matrix-bot-password", "password"),
        ],
        ports=[
            k8s.ContainerPort(name="proxy", container_port=PROXY_PORT),
            k8s.ContainerPort(name="metrics", container_port=_METRICS_PORT),
        ],
        security_context=k8s.SecurityContext(
            allow_privilege_escalation=False, capabilities=k8s.Capabilities(drop=["ALL"])
        ),
        volume_mounts=[
            k8s.VolumeMount(name="config", mount_path="/etc/iron-proxy", read_only=True),
            k8s.VolumeMount(name="ca", mount_path="/ca", read_only=True),
        ],
        resources=k8s.ResourceRequirements(
            requests={"cpu": k8s.Quantity.from_string("50m"), "memory": k8s.Quantity.from_string("128Mi")},
            limits={"cpu": k8s.Quantity.from_string("500m"), "memory": k8s.Quantity.from_string("512Mi")},
        ),
    )


def _deployment(scope: Construct) -> None:
    k8s.KubeDeployment(
        scope,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=NAME, namespace=NAMESPACE, labels=LABELS, annotations={"reloader.stakater.com/auto": "true"}
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=LABELS),
                spec=k8s.PodSpec(
                    # Private package in the in-cluster Forgejo registry. The credential is
                    # reflected into this namespace by cluster/k8s/forgejo-images/.
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    # Matches haku-{claude-oauth,openclaw-spike}-proxy, which run the same image.
                    # This namespace sets no pod-security.kubernetes.io labels, so only the
                    # cluster-default baseline applies and none of this is enforced for us -- it
                    # has to be stated here. The deliberate waiver for this agent is about egress
                    # destinations; it does not extend to pod security, and this pod holds the
                    # densest credential set of the three proxies.
                    automount_service_account_token=False,
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True,
                        run_as_user=65532,
                        run_as_group=65532,
                        seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                    ),
                    containers=[_container()],
                    volumes=[
                        k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name=_CONFIG_MAP_NAME)),
                        # iron-proxy reads the CA certificate and key straight from these paths, so
                        # unlike mitmproxy it needs no initContainer to assemble a combined PEM. It
                        # rejects a CA without keyCertSign at startup, loudly.
                        k8s.Volume(name="ca", secret=k8s.SecretVolumeSource(secret_name=_CA_SECRET_NAME)),
                    ],
                ),
            ),
        ),
    )


def _service(scope: Construct) -> None:
    k8s.KubeService(
        scope,
        "service",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAMESPACE),
        spec=k8s.ServiceSpec(
            selector=LABELS,
            ports=[
                k8s.ServicePort(name="proxy", port=PROXY_PORT, target_port=k8s.IntOrString.from_number(PROXY_PORT)),
                # iron-proxy's Prometheus metrics. mitmproxy's `mitmweb` UI on 8081 goes with it
                # -- it was never routed, and a browsable log of the agent's intercepted traffic
                # is not a thing to leave reachable in-cluster.
                k8s.ServicePort(
                    name="metrics", port=_METRICS_PORT, target_port=k8s.IntOrString.from_number(_METRICS_PORT)
                ),
            ],
        ),
    )


def _ingress_policy(scope: Construct) -> None:
    """The proxy confers every credential it mediates to callers that present the corresponding
    non-secret placeholder. Keep that capability reachable only from the two intended clients:
    the OpenClaw Agent pod and its KubeVirt devbox. In particular, namespace co-tenancy is not
    authority to use this Service, and the metrics port remains closed until a reviewed scraper
    needs it."""
    cilium.network_policy(
        scope,
        "ingress",
        metadata=metadata("allow-public-coder-agent-proxy-ingress", NAMESPACE),
        selector=LABELS,
        ingress=[
            cilium.ingress_from(
                # Spelled here: public_coder_agent_config imports this module for the proxy's address.
                _endpoint(NAMESPACE, {"app.kubernetes.io/name": "public-coder-agent"}),
                _endpoint(public_coder_devbox.NAMESPACE, public_coder_devbox.POD_LABELS),
                ports=[PROXY_PORT],
            )
        ],
    )


def _egress_policy(scope: Construct) -> None:
    """Egress for the proxy pod -- deliberately unrestricted, for THIS agent only.

    Its job is opening pull requests against arbitrary public repositories and reading whatever
    they link to, so a maintained domain list is friction with little left to protect. What such
    a list would have protected is now protected elsewhere and better: the GitHub PAT lives in
    the proxy, the agent never possesses it, and iron.yaml scopes the substitution by host -- so
    however far the agent can reach, the token is attached to GitHub and nowhere else.

    The real cost of widening this is a data boundary, not a credential one: with the world
    reachable, a prompt injection from a cloned repository can send repository contents or
    session memory somewhere. That is the accepted trade here, and only here.

    **Domain confinement remains a requirement for agents that will handle higher-sensitivity
    material.** The confined list is kept below and is one edit away -- re-enabling it means
    uncommenting the allowlist transform in iron.yaml too, so the two stay in step. Evidence that
    the confined shape works: docs/personal_agents/findings/egress_and_tls.md F4, F15, F16.

    NOT relaxed, and not relaxable without losing the credential boundary: the app's own
    NetworkPolicy, which permits egress solely to this proxy. That is what makes the substitution
    unavoidable rather than advisory.
    """
    cilium.network_policy(
        scope,
        "egress",
        metadata=metadata("allow-public-coder-agent-proxy-egress", NAMESPACE),
        selector=LABELS,
        egress=[
            cilium.dns_egress(protocols=["ANY"], resolves=["*"]),
            # `world` alone does not mean "everywhere". Cilium carves the cluster's own nodes out
            # of it: every `*.allegedly.works` name resolves to the five OVH node ExternalIPs
            # (Envoy binds 80/443 there in hostNetwork mode), and those addresses carry
            # `reserved:remote-node`, not `reserved:world`. A `world`-only rule therefore
            # black-holes the SYN and the dial times out -- which is how this proxy could reach the
            # entire internet yet not haku.allegedly.works. `host` is here too because this
            # Deployment has no nodeSelector: on an OVH node, that node's own IP is
            # `reserved:host`. Widening a CIDR/FQDN rule cannot substitute --
            # `policy-cidr-match-mode` is unset cluster-wide, so CIDR-derived selectors never match
            # node IPs. See cluster/docs/cilium_network_policy.md.
            cilium.egress_to_entities("world", "remote-node", "host", ports=[443, 80]),
            # The agent's normalized analytics reads leave the app through this Iron proxy, then
            # use the private ClickHouse HTTP ClusterIP service. Do not grant this egress to the
            # app Pod itself.
            cilium.egress_to(_endpoint(client.NAMESPACE, client.LABELS), client.HTTP_PORT),
            # The confined configuration, for restoration: TCP 443 by toFQDNs to the GitHub hosts
            # (clone, push to forks, and open pull requests via the REST API) github.com,
            # api.github.com, codeload.github.com, objects.githubusercontent.com,
            # release-assets.githubusercontent.com (release asset redirects, e.g. workspace-local
            # tool bootstraps) and raw.githubusercontent.com; and to the package indexes for
            # in-task tooling (pip install / npx) pypi.org, files.pythonhosted.org and
            # registry.npmjs.org.
        ],
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    _external_secrets(chart)
    _ca(chart)
    _deployment(chart)
    _service(chart)
    _ingress_policy(chart)
    _egress_policy(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
