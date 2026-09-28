"""The ClusterPolicies that inject an egress proxy's env and CA bundle into every Pod
created in a sandbox namespace.

Both use RFC 6902 JSON patches (not patchStrategicMerge) to avoid volume merge
conflicts with Kyverno autogen + Flux server-side dry-run on Deployments.

Autogen is disabled: JSON patch "add /-" always appends, so if autogen mutates the
Deployment template AND the Pod-level rule fires at Pod creation, volumes/env/mounts
get duplicated. With autogen off, only Pod admission is mutated.

All rules restrict to CREATE operations only. The "add /-" append has no idempotency
check of its own — if the policy re-evaluates on UPDATE, it appends the same
volume/env/mount a second time and the API server rejects the request (duplicate
volume name / mount path / env var). That blocked the Kubernetes Job controller from
PATCHing completed pods to remove the batch.kubernetes.io/job-tracking finalizer,
which prevented Jobs from transitioning to Complete.

CREATE-only is NOT sufficient on its own, which is why every rule also carries a
precondition asserting the thing it is about to append is not already there.
Kyverno's webhook is registered `reinvocationPolicy: IfNeeded`, so a webhook ordered
after it that mutates the pod causes Kyverno to run AGAIN within the SAME CREATE,
reading its own output as input. "add /-" appends unconditionally, so the second pass
duplicated the volume and its mount, and the API server rejects a duplicate volume
name outright:

  Pod "haku-ui-0" is invalid: [spec.volumes[3].name: Duplicate value:
  "haku-egress-proxy-ca-cert", spec.containers[0].volumeMounts[3].mountPath:
  Invalid value: "/egress-proxy-ca": must be unique]

That was a real outage (2026-08-05): haku-sandbox runs VPA in Auto mode, VPA's webhook
sorts after Kyverno's and patches resource requests, and haku-ui sat at 1/2 with
haku-ui-0 permanently uncreatable. It presented as intermittent because VPA patches
nothing when its recommendation already matches the pod. Pinned by
//cluster/validation/kyverno:test_proxy_injection, which applies each policy twice.

NO_PROXY is one flat string, injected identically into containers and initContainers.
Entry by entry, the cluster-addressing set both policies share:

  127.0.0.1, localhost    Loopback. A pod's own sidecars, health checks and
                          127.0.0.1-bound helpers must not be shipped to a
                          proxy living in another pod.
  .svc                    In-cluster Service names, in all three forms clients
  .svc.cluster.local      actually construct. Suffix matching is literal: the
  kubernetes.default.svc  short in-cluster forms do NOT match the fully-qualified
                          suffix, so `https://kubernetes.default.svc` — the default
                          apiserver URL every in-cluster client library builds —
                          went to the proxy and hung until it timed out (found
                          from a haku sandbox, 2026-07-24).
  10.0.0.0/8              Pod and Service CIDRs — the same in-cluster
                          destinations, reached by IP instead of by name (a
                          kubeconfig with a ClusterIP server, a hard-coded pod
                          IP). Broadest entry here; it is what makes the list
                          "all cluster addressing" rather than an enumeration.

Everything on the list reaches its destination with no proxy in the path and no
egress allowlist applied. That is accepted rather than overlooked: each entry is
cluster-internal or cluster-published, and those services authenticate their own
callers (bearer, mTLS, SSO). The fence these policies set up is a fence over the
public internet.
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
                            )
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


def inject_mitmproxy_chart(app: App) -> Chart:
    """Makes any pod in claude-sandbox (e.g. `kubectl run --image=curlimages/curl`) work
    turnkey through the shared mitmproxy, without the pod spec knowing about the proxy.

    NO_PROXY is the shared cluster-addressing set only. `*.allegedly.works`, which
    inject-haku-egress-proxy bypasses, is deliberately still routed through this proxy.
    Whether to unify it with the other injection policy's bypass set is an open
    decision; nothing in claude-sandbox depends on the current routing.
    """
    chart = Chart(app, "inject-mitmproxy", disable_resource_name_hashes=True)
    _injection_policy(
        chart,
        name="inject-mitmproxy",
        title="Inject mitmproxy",
        description=(
            "Mutates pods in claude-sandbox to automatically inject mitmproxy proxy env vars (HTTP_PROXY, "
            "HTTPS_PROXY, NO_PROXY, SSL_CERT_FILE, CURL_CA_BUNDLE, REQUESTS_CA_BUNDLE, NODE_EXTRA_CA_CERTS) and "
            "mount the mitmproxy trust bundle at /mitmproxy-ca/ca-certificates.crt."
        ),
        namespace="claude-sandbox",
        volume="mitmproxy-ca-cert",
        mount_path="/mitmproxy-ca",
        proxy_url="http://mitmproxy.agents-mitmproxy.svc.cluster.local:8080",
        no_proxy=f"127.0.0.1,localhost,{_CLUSTER_NO_PROXY}",
    )
    return chart


def inject_haku_egress_proxy_chart(app: App) -> Chart:
    """The haku-sandbox counterpart of inject-mitmproxy, pointing at haku-sandbox's own
    proxy and CA so the two sandboxes stay isolated.

    NO_PROXY adds `*.allegedly.works` and `.allegedly.works` to the shared set: our own
    public names. They resolve to the public hostNetwork Cilium Gateway IPs, but they are
    still cluster-owned services. If haku-egress-proxy CONNECTs to one of them, DNS can
    pick the proxy pod's own Gateway node; that same-node public-Gateway hairpin returns
    Envoy "Access denied" for the proxy identity. They are bypassed for that reason, not
    because they are trusted more than the internet: direct sandbox egress to them is
    still bounded by Cilium's cluster-only egress rule. Both forms because NO_PROXY
    matching differs across curl, Go, Python, node and git.

    In-cluster Forgejo (forgejo-http.forgejo) is deliberately NOT on the list: the
    sandbox holds only the inert haku-forgejo-token-placeholder, and the fence is what
    swaps it for the real credential at the Forgejo origins (haku/console config.yaml,
    haku_forgejo_token). A bypass here sends git straight to Forgejo with the placeholder
    and every clone fails "Credentials are incorrect or have expired". Gotcha: the `.svc`
    entries still bypass forgejo-http.forgejo.svc.cluster.local, so anything that needs
    the credential must use the short service name, as haku-sandbox-setup.sh does.
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
