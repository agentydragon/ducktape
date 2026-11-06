"""Inject proxy environment and CA mounts into Pods in a sandbox namespace.

RFC 6902 patches avoid the volume merge conflicts between strategic-merge patches,
Kyverno autogen and Flux Deployment dry-runs. `add /-` appends unconditionally.
Disable autogen so Deployment-template and Pod admission cannot both append wiring.
Restrict to CREATE: reapplying on UPDATE once blocked completed Pods' job-tracking
finalizer removal and Job completion.

CREATE-only also needs absence preconditions on every appended volume/mount.
With `reinvocationPolicy: IfNeeded`, a later webhook can mutate the Pod and rerun
Kyverno on its own output within the same CREATE. On 2026-08-05, VPA Auto's later
resource-request patch left haku-ui at 1/2 with an uncreatable Pod due to duplicate
volume/mount paths. It appeared intermittent because VPA did nothing when requests
already matched its recommendation. The real-CLI regression
`//cluster/cdk8s/kyverno:test_proxy_injection` applies each policy twice.

NO_PROXY is one flat string for containers and initContainers. Shared entries:

- `127.0.0.1,localhost`: sidecars, health checks and loopback-only helpers.
- `.svc,.svc.cluster.local,kubernetes.default.svc`: all Service-name forms clients
  use. Literal suffix matching means the short apiserver URL does not match the
  fully qualified suffix; omitting it sent in-cluster API traffic to the proxy and
  hung (observed from a Haku sandbox, 2026-07-24).
- `10.0.0.0/8`: Pod/Service addresses reached by IP, including ClusterIP kubeconfigs.
  This intentionally covers cluster addressing beyond enumerated hostnames.

Every listed destination bypasses the proxy and its egress allowlist. This is an
accepted boundary: cluster-internal/published services authenticate their own callers
(bearer, mTLS or SSO); the proxy allowlist targets public-internet traffic.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from kyverno_clusterpolicy_crds.io.kyverno import (
    ClusterPolicySpecRules,
    ClusterPolicySpecRulesMatch,
    ClusterPolicySpecRulesMatchAnyResources,
    ClusterPolicySpecRulesMatchAnyResourcesOperations,
    ClusterPolicySpecRulesMutate,
    ClusterPolicySpecRulesMutateForeach,
    ClusterPolicySpecRulesMutateForeachPreconditions,
    ClusterPolicySpecRulesMutateForeachPreconditionsAll,
    ClusterPolicySpecRulesMutateForeachPreconditionsAllOperator,
)

from cluster.cdk8s.providers.kyverno.cluster_policy import ClusterPolicy, match_resources

_CLUSTER_NO_PROXY = ".svc,.svc.cluster.local,kubernetes.default.svc,10.0.0.0/8"
# A container that sets either of these has chosen its own egress path; the injection skips it.
_OWN_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY")


def _pods_created_in(namespace: str) -> ClusterPolicySpecRulesMatch:
    return match_resources(
        ClusterPolicySpecRulesMatchAnyResources(
            kinds=["Pod"], namespaces=[namespace], operations=[ClusterPolicySpecRulesMatchAnyResourcesOperations.CREATE]
        )
    )


def _env_and_mount_rule(
    *,
    rule: str,
    field: str,
    preconditions: dict[str, object] | None,
    namespace: str,
    volume: str,
    mount_path: str,
    proxy_url: str,
    no_proxy: str,
) -> ClusterPolicySpecRules:
    """Appends the proxy env vars and the CA mount to every entry of `spec.<field>`,
    skipping an entry that already mounts the CA (the reinvocation guard)."""
    ca_file = f"{mount_path}/ca-certificates.crt"
    # Four CA variables because four client stacks each read only their own (OpenSSL/Python
    # ssl, curl, `requests`, Node); a missing one passes admission and fails TLS later, in
    # whichever runtime uses that client.
    env = [
        ("HTTP_PROXY", f'"{proxy_url}"'),
        ("HTTPS_PROXY", f'"{proxy_url}"'),
        ("NO_PROXY", f'"{no_proxy}"'),
        ("SSL_CERT_FILE", ca_file),
        ("CURL_CA_BUNDLE", ca_file),
        ("REQUESTS_CA_BUNDLE", ca_file),
        ("NODE_EXTRA_CA_CERTS", ca_file),
    ]
    element = f"/spec/{field}/{{{{elementIndex}}}}"
    patches = [
        f'- op: add\n  path: "{element}/env/-"\n  value:\n    name: {name}\n    value: {value}' for name, value in env
    ]
    patches.append(
        f'- op: add\n  path: "{element}/volumeMounts/-"\n  value:\n'
        f"    name: {volume}\n    mountPath: {mount_path}\n    readOnly: true"
    )
    return ClusterPolicySpecRules(
        name=rule,
        match=_pods_created_in(namespace),
        preconditions=preconditions,
        mutate=ClusterPolicySpecRulesMutate(
            foreach=[
                ClusterPolicySpecRulesMutateForeach(
                    list=f"request.object.spec.{field}",
                    preconditions=ClusterPolicySpecRulesMutateForeachPreconditions(
                        all=[
                            ClusterPolicySpecRulesMutateForeachPreconditionsAll(
                                key=f"{{{{ (element.volumeMounts || `[]`)[?name=='{volume}'] | length(@) }}}}",
                                operator=ClusterPolicySpecRulesMutateForeachPreconditionsAllOperator.EQUALS,
                                value=0,
                            ),
                            # A container that names its own proxy is left alone: the env here is a default for
                            # pods that say nothing, not an override of ones with an egress path of their own.
                            *(
                                ClusterPolicySpecRulesMutateForeachPreconditionsAll(
                                    key=f"{{{{ (element.env || `[]`)[?name=='{name}'] | length(@) }}}}",
                                    operator=ClusterPolicySpecRulesMutateForeachPreconditionsAllOperator.EQUALS,
                                    value=0,
                                )
                                for name in _OWN_PROXY_VARS
                            ),
                        ]
                    ),
                    patches_json6902="\n".join(patches),
                )
            ]
        ),
    )


def _injection_policy(
    chart: Chart,
    *,
    name: str,
    title: str,
    description: str,
    namespace: str,
    volume: str,
    mount_path: str,
    proxy_url: str,
    no_proxy: str,
) -> None:
    ClusterPolicy(
        chart,
        "policy",
        metadata=ApiObjectMetadata(
            name=name,
            annotations={
                "pod-policies.kyverno.io/autogen-controllers": "none",
                "policies.kyverno.io/title": title,
                "policies.kyverno.io/category": "Networking",
                "policies.kyverno.io/severity": "medium",
                "policies.kyverno.io/subject": "Pod",
                "policies.kyverno.io/description": description,
            },
        ),
        mutate_existing_on_policy_update=False,
        rules=[
            ClusterPolicySpecRules(
                name="add-proxy-volume",
                match=_pods_created_in(namespace),
                preconditions={
                    "all": [
                        {
                            "key": f"{{{{ (request.object.spec.volumes || `[]`)[?name=='{volume}'] | length(@) }}}}",
                            "operator": "Equals",
                            "value": 0,
                        }
                    ]
                },
                mutate=ClusterPolicySpecRulesMutate(
                    patches_json6902=(
                        f'- op: add\n  path: "/spec/volumes/-"\n  value:\n'
                        f"    name: {volume}\n    configMap:\n      name: {volume}"
                    )
                ),
            ),
            _env_and_mount_rule(
                rule="add-proxy-env-and-mount-containers",
                field="containers",
                preconditions=None,
                namespace=namespace,
                volume=volume,
                mount_path=mount_path,
                proxy_url=proxy_url,
                no_proxy=no_proxy,
            ),
            _env_and_mount_rule(
                rule="add-proxy-env-and-mount-init-containers",
                field="initContainers",
                preconditions={
                    "any": [
                        {
                            "key": "{{ (request.object.spec.initContainers || `[]`) | length(@) }}",
                            "operator": "GreaterThanOrEquals",
                            "value": 1,
                        }
                    ]
                },
                namespace=namespace,
                volume=volume,
                mount_path=mount_path,
                proxy_url=proxy_url,
                no_proxy=no_proxy,
            ),
        ],
    )


def inject_haku_egress_proxy_chart(app: App) -> Chart:
    """Makes any pod in haku-sandbox work turnkey through haku-egress-proxy, without the pod
    spec knowing about the proxy.

    NO_PROXY adds `*.allegedly.works` and `.allegedly.works` to the shared set: our own
    public names. They resolve to the public hostNetwork Cilium Gateway IPs, but they are
    still cluster-owned services. If haku-egress-proxy CONNECTs to one of them, DNS can
    pick the proxy pod's own Gateway node; that same-node public-Gateway hairpin returns
    Envoy "Access denied" for the proxy identity. They are bypassed for that reason, not
    because they are trusted more than the internet: direct sandbox egress to them is
    still bounded by Cilium's cluster-only egress rule. Both forms because NO_PROXY
    matching differs across curl, Go, Python, node and git.

    In-cluster Services, Forgejo included, are reached directly: the `.svc` and `.svc.cluster.local`
    entries bypass the proxy for them. (This docstring used to explain keeping Forgejo off the list so the
    fence could swap a placeholder for the real credential; this proxy has never had a credential-swapping
    addon, so workloads that need Forgejo credentials mount them.)

    The env is a default, not an override: a container that already sets `HTTP_PROXY` or `HTTPS_PROXY` is
    skipped entirely (no env, no CA mount), so a workload with its own egress path (an Agentplane sidecar, a
    different proxy) just declares it. Before 2026-11-02 the env was appended to every container and the last
    duplicate won, which silently overrode such pods (haku-ui's calls skipped its sidecar and Home Assistant
    answered 401). Today the only workload here that needs this proxy is haku-anki, for AnkiWeb sync; it is
    moving behind an Agentplane sidecar (the `ankiweb-sync` policy in actions_staging_policies.py), after which nothing in haku-sandbox does.
    """
    chart = Chart(app, "inject-haku-egress-proxy", disable_resource_name_hashes=True)
    _injection_policy(
        chart,
        name="inject-haku-egress-proxy",
        title="Inject haku-egress-proxy",
        description=(
            "Mutates pods in haku-sandbox to automatically inject the dedicated haku-egress-proxy proxy env vars "
            "(HTTP_PROXY, HTTPS_PROXY, NO_PROXY, SSL_CERT_FILE, CURL_CA_BUNDLE, REQUESTS_CA_BUNDLE, "
            "NODE_EXTRA_CA_CERTS) and mount the haku-egress-proxy trust bundle at "
            "/egress-proxy-ca/ca-certificates.crt."
        ),
        namespace="haku-sandbox",
        volume="haku-egress-proxy-ca-cert",
        mount_path="/egress-proxy-ca",
        proxy_url="http://haku-egress-proxy.haku-egress-proxy.svc.cluster.local:8080",
        no_proxy=f"127.0.0.1,localhost,*.allegedly.works,.allegedly.works,{_CLUSTER_NO_PROXY}",
    )
    return chart
