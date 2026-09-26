"""Authentik itself, rendered into `cluster/k8s/authentik/app`: the Helm release, its host
ConfigMap, the public HTTPRoute, the server's ingress policy and its PodMonitor.

Also its `kustomization.yaml`, listing this file beside the hand-written SOPS Secrets and
rendering every hand-written `blueprints/*.yaml` into the `authentik-sso-blueprints` ConfigMap.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallStrategy,
    HelmReleaseSpecInstallStrategyName,
    HelmReleaseSpecUpgrade,
    HelmReleaseSpecUpgradeStrategy,
    HelmReleaseSpecUpgradeStrategyName,
)
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec
from gateway_api_crds.io.k8s.networking.gateway import (
    HttpRoute,
    HttpRouteSpec,
    HttpRouteSpecRules,
    HttpRouteSpecRulesBackendRefs,
    HttpRouteSpecRulesFilters,
    HttpRouteSpecRulesFiltersResponseHeaderModifier,
    HttpRouteSpecRulesFiltersResponseHeaderModifierSet,
    HttpRouteSpecRulesFiltersType,
)
from prometheus_operator_podmonitor_crds.com.coreos.monitoring import (
    PodMonitor,
    PodMonitorSpec,
    PodMonitorSpecPodMetricsEndpoints,
    PodMonitorSpecSelector,
)

from cluster.cdk8s.authentik import db
from cluster.cdk8s.flux import ConfigMapArgs, GeneratorOptions, kustomize_kustomization
from cluster.cdk8s.gateway import cluster_gateway_parent_ref
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.helm import helm_release
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.providers.cilium.network_policy import IngressRule, NetworkPolicy
from util.bazel.runfiles import get_required_path, own_repo_rlocation

NAME = "authentik"
NAMESPACE = "authentik"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/authentik/app"
_HOST_CONFIG_MAP = "authentik-host"
_BLUEPRINTS_CONFIG_MAP = "authentik-sso-blueprints"
_SOPS_SECRETS = (
    "admin-password",
    "secret-key",
    "bootstrap-token",
    "user-password",
    "google-oauth",
    "auragon-google-email",
)
_SERVER_LABELS = {"app.kubernetes.io/component": "server", "app.kubernetes.io/name": NAME}
# Pod ports: 9000 (HTTP), 9443 (HTTPS), 9300 (metrics).
_HTTP, _HTTPS, _METRICS = 9000, 9443, 9300


def _pod_env() -> dict[str, object]:
    """The SOPS-managed secrets (secret key, bootstrap password/token, user password) and the
    database password, shared by the server and the worker."""
    return {
        "envFrom": [
            {"configMapRef": {"name": _HOST_CONFIG_MAP}},
            {"secretRef": {"name": "authentik-bootstrap"}},
            {"secretRef": {"name": "authentik-admin-password"}},
            {"secretRef": {"name": "authentik-secret-key"}},
            {"secretRef": {"name": "authentik-user-password"}},
        ],
        "env": [
            {
                "name": "AUTHENTIK_POSTGRESQL__PASSWORD",
                "valueFrom": {"secretKeyRef": {"name": db.CREDENTIALS_SECRET, "key": "password"}},
            }
        ],
    }


def _spread(component: str) -> dict[str, object]:
    """One replica per node where possible; preferred, so a surge pod may still land on an
    occupied node."""
    return {
        "deploymentStrategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}},
        "affinity": {
            "podAntiAffinity": {
                "preferredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "weight": 100,
                        "podAffinityTerm": {
                            "labelSelector": {
                                "matchLabels": {
                                    "app.kubernetes.io/component": component,
                                    "app.kubernetes.io/instance": NAME,
                                }
                            },
                            "topologyKey": "kubernetes.io/hostname",
                        },
                    }
                ]
            }
        },
    }


# No CPU limits (cluster/docs/decisions.md § CPU limits policy): tofu-controller's
# Authentik-provider refreshes held the server at a 500m limit, and operator token exchanges
# queued behind them into their 10s timeout. CPU requests are about the goldilocks VPA
# targets; the namespace keeps VPA to requests only.
_SERVER_RESOURCES = {"requests": {"cpu": "300m", "memory": "512Mi"}, "limits": {"memory": "1536Mi"}}
_WORKER_RESOURCES = {"requests": {"cpu": "300m", "memory": "512Mi"}, "limits": {"memory": "1Gi"}}


def _values() -> dict[str, object]:
    return {
        "global": {
            "deploymentAnnotations": {
                "secret.reloader.stakater.com/reload": f"{db.CREDENTIALS_SECRET},authentik-user-password",
                "configmap.reloader.stakater.com/reload": _BLUEPRINTS_CONFIG_MAP,
            }
        },
        # The worker processes the blueprints natively from this ConfigMap.
        "blueprints": {"configMaps": [_BLUEPRINTS_CONFIG_MAP]},
        "authentik": {
            # The empty secrets below arrive via envFrom (`_pod_env`) instead.
            "secret_key": "",
            "error_reporting": {"enabled": False},
            "postgresql": {"host": f"{db.NAME}-rw", "name": db.DATABASE, "user": db.DATABASE, "password": ""},
            "redis": {"host": ""},
            "bootstrap": {"password": "", "token": ""},
        },
        # A single replica: OVH memory is too tight for two with these requests.
        "server": {
            "name": "server",
            "replicas": 1,
            # Beside the database: Django issues many serialized queries per request, and an
            # unpinned server once landed at home, 114ms from the OVH primary, where the static
            # OIDC discovery document took ~1.6s. It also made `Home down` take SSO with it.
            "nodeSelector": dict(db.NODE_SELECTOR),
            **_spread("server"),
            # 20 minutes for first-boot migrations: a startup kill mid-migration leaves the
            # connection idle-in-transaction and blocks the next attempt.
            "startupProbe": {"failureThreshold": 120, "periodSeconds": 10},
            # /-/health/live/ returns 500 whenever PostgreSQL is unreachable, so the default
            # threshold turned every seconds-long DB blip into a ~90s reboot and 503s
            # (cluster/debug/2026_06_claude_ai_connector_deauth.md). Ride out ~2min; readiness stays
            # fast so the pod leaves the Service at once. Outpost API calls regularly take 1s+,
            # hence the 10s timeouts.
            "livenessProbe": {"timeoutSeconds": 10, "periodSeconds": 15, "failureThreshold": 8},
            "readinessProbe": {"timeoutSeconds": 10},
            **_pod_env(),
            "service": {"enabled": True, "type": "ClusterIP", "port": 80},
            # Routed by the HTTPRoute below.
            "ingress": {"enabled": False},
            "resources": _SERVER_RESOURCES,
        },
        "worker": {
            "name": "worker",
            "replicas": 1,
            # As DB-bound as the server.
            "nodeSelector": {"topology.kubernetes.io/region": "hil"},
            **_spread("worker"),
            **_pod_env(),
            "resources": _WORKER_RESOURCES,
        },
        # The external CNPG cluster in `db.py`.
        "postgresql": {"enabled": False},
        # TODO: replace this chart-bundled Redis with an operator-managed
        # RedisReplication (redis.redis.opstreelabs.in/v1beta2), as grocy and tana-mcp do.
        # Then set redis.enabled: false and point Authentik at the operator-managed Valkey
        # via AUTHENTIK_REDIS__HOST / AUTHENTIK_REDIS__PASSWORD.
        "redis": {"enabled": True},
        "serviceAccount": {"create": True, "name": "authentik"},
    }


def _helm_release(chart: Chart) -> None:
    repository = HelmRepository(
        chart,
        "repository",
        metadata=metadata(NAME, NAMESPACE),
        spec=HelmRepositorySpec(interval="24h", url="https://charts.goauthentik.io"),
    )
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=repository,
        chart="authentik",
        version="2026.8.2",
        interval="15m",
        timeout="15m",
        install=HelmReleaseSpecInstall(
            strategy=HelmReleaseSpecInstallStrategy(name=HelmReleaseSpecInstallStrategyName.RETRY_ON_FAILURE)
        ),
        upgrade=HelmReleaseSpecUpgrade(
            strategy=HelmReleaseSpecUpgradeStrategy(name=HelmReleaseSpecUpgradeStrategyName.RETRY_ON_FAILURE)
        ),
        values=_values(),
    )


def _host_config_map(chart: Chart) -> None:
    k8s.KubeConfigMap(
        chart,
        "host",
        metadata=k8s.ObjectMeta(name=_HOST_CONFIG_MAP, namespace=NAMESPACE),
        data={
            "AUTHENTIK_HOST": "https://auth.allegedly.works",
            # Cilium Gateway API uses host-network TPROXY, so Gateway hairpins can reach Authentik
            # with the caller's cluster-pod source address instead of the Envoy node address.
            # Authentik 2026.8 otherwise ignores X-Forwarded-Proto and emits HTTP OIDC metadata for
            # an HTTPS public request. The CiliumNetworkPolicy below remains the reachability
            # boundary for Authentik's HTTP ports.
            "AUTHENTIK_LISTEN__TRUSTED_PROXY_CIDRS": "10.42.0.0/24,10.244.0.0/16",
        },
    )


def _http_route(chart: Chart) -> None:
    HttpRoute(
        chart,
        "route",
        metadata=metadata(NAME, NAMESPACE),
        spec=HttpRouteSpec(
            parent_refs=[cluster_gateway_parent_ref()],
            hostnames=["auth.allegedly.works"],
            rules=[
                # Let the operator-owned Haku console (haku.allegedly.works) frame Authentik's
                # pages, so the agent-authored Haku UI iframe (haku-ui.allegedly.works, a separate
                # Authentik proxy app) can complete its first-time auth in-frame off the existing
                # SSO session instead of forcing a separate top-level login. Authentik defaults to
                # `X-Frame-Options: DENY` and sets no CSP; we swap that for a `frame-ancestors`
                # whitelist so ONLY the console (and Authentik itself) may frame it — every other
                # origin still can't. This does not let the framed app act as the console: that is
                # cross-origin isolation (separate origins/cookies), independent of framing headers.
                HttpRouteSpecRules(
                    filters=[
                        HttpRouteSpecRulesFilters(
                            type=HttpRouteSpecRulesFiltersType.RESPONSE_HEADER_MODIFIER,
                            response_header_modifier=HttpRouteSpecRulesFiltersResponseHeaderModifier(
                                remove=["X-Frame-Options"],
                                set=[
                                    HttpRouteSpecRulesFiltersResponseHeaderModifierSet(
                                        name="Content-Security-Policy",
                                        value="frame-ancestors 'self' https://haku.allegedly.works",
                                    )
                                ],
                            ),
                        )
                    ],
                    backend_refs=[HttpRouteSpecRulesBackendRefs(name="authentik-server", port=80)],
                )
            ],
        ),
    )


def _namespace_source(namespace: str) -> dict[str, str]:
    return {"k8s:io.kubernetes.pod.namespace": namespace}


def _network_policy(chart: Chart) -> None:
    # Unauthenticated /api/v3/ enables user enumeration, provider tampering and backdoor user
    # creation, so only these consumers reach the server. A CiliumNetworkPolicy, since a stock
    # NetworkPolicy cannot match Gateway API traffic (reserved:ingress).
    NetworkPolicy(
        chart,
        "server-ingress",
        metadata=metadata("authentik-server-ingress", NAMESPACE),
        selector=_SERVER_LABELS,
        ingress=[
            IngressRule.from_gateway(_HTTP, _HTTPS),
            # Outposts sync their config from the server API.
            IngressRule.from_endpoints(_namespace_source(NAMESPACE), ports=[_HTTP, _HTTPS, _METRICS]),
            # tofu-controller's tf-runner calls the Authentik API.
            IngressRule.from_endpoints(_namespace_source("flux-system"), ports=[_HTTP]),
            # Grafana OIDC token exchange and Prometheus scraping.
            IngressRule.from_endpoints(_namespace_source("monitoring"), ports=[_HTTP, _METRICS]),
            # Gatus liveness probes.
            IngressRule.from_endpoints(_namespace_source("gatus"), ports=[_HTTP]),
            # The agentplane app and Action Service use the public issuer so discovery returns the
            # canonical external endpoints. When the public hostname resolves to the caller's own
            # node, hostNetwork Gateway hairpin traffic can arrive with the caller's namespace
            # identity instead of reserved:ingress; admitting this namespace on the HTTP ports
            # keeps that path equivalent to the Gateway route.
            IngressRule.from_endpoints(_namespace_source("agentplane-staging"), ports=[_HTTP, _HTTPS]),
            # kubectl MCP servers: OIDC discovery and JWKS.
            IngressRule.from_endpoints(_namespace_source("kubectl-passthrough-mcp"), ports=[_HTTP]),
            # The Claude agent reads the Authentik API (events, apps, users, outposts, flows,
            # policies) with the AUTHENTIK_API_TOKEN loaded at session start.
            IngressRule.from_endpoints(_namespace_source("claude-sandbox"), ports=[_HTTP]),
        ],
    )


def _pod_monitor(chart: Chart) -> None:
    PodMonitor(
        chart,
        "server-podmonitor",
        metadata=metadata("authentik-server", NAMESPACE),
        spec=PodMonitorSpec(
            selector=PodMonitorSpecSelector(match_labels=_SERVER_LABELS),
            # TODO: Consider adding bearer token auth if Authentik metrics require authentication.
            pod_metrics_endpoints=[PodMonitorSpecPodMetricsEndpoints(port="metrics", path="/metrics")],
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    _helm_release(chart)
    _host_config_map(chart)
    _http_route(chart)
    _network_policy(chart)
    _pod_monitor(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    # The data dep packages exactly Bazel's glob of the directory, so a new blueprint is listed
    # by regenerating.
    blueprints = get_required_path(own_repo_rlocation(f"{OUTPUT_DIR}/blueprints"))
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(
            resources=[f"{NAME}.k8s.yaml", *(f"{secret}.sops.yaml" for secret in _SOPS_SECRETS)],
            config_map_generator=[
                ConfigMapArgs(
                    name=_BLUEPRINTS_CONFIG_MAP,
                    namespace=NAMESPACE,
                    # The Helm values name the ConfigMap, and kustomize cannot rewrite a reference
                    # inside a HelmRelease's values.
                    options=GeneratorOptions(disable_name_suffix_hash=True),
                    files=[f"blueprints/{path.name}" for path in sorted(blueprints.glob("*.yaml"))],
                )
            ],
        ),
    )
