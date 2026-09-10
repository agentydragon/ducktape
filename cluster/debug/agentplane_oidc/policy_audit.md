# Cilium policy audit — 2026-09-10

Repository baseline: `76b17f32510f41c8ea428c4ee15da42b8da606ad`.
Read-only live inventory and probes: approximately 04:00–04:10 UTC.
No live configuration changes or credential-bearing requests.

## Findings

### A. Actions repeats Agentplane's problematic Authentik path

Source: `cluster/k8s/agentplane-staging/actions/networkpolicy.yaml:57`.
The live policy grants host/remote-node TCP 443 with SNI
`auth.allegedly.works`, but no Authentik backend permission. The comment
identifies canonical-issuer JWKS retrieval as its purpose.

Actions Pod `agentplane-actions-6669c695b6-pqp8m`, IP `10.244.4.48`, runs on
`ovh-ns102453`. Cilium endpoint 3578, identity 47832. Credential-free probes
used its existing network namespace, retained TLS certificate verification,
and requested public Agentplane OIDC discovery with the canonical Host/SNI:

| Destination                        | Observed result      |
| ---------------------------------- | -------------------- |
| Local node `147.135.37.175:443`    | TLS reset, errno 104 |
| Remote node `147.135.39.176:443`   | HTTP 200             |
| Gateway Service `10.106.122.5:443` | HTTP 403             |

This establishes another workload exposed to the failing same-node path and
another missing permission for a Service-path migration. The omission explains
the Service denial, not the node-IP TLS reset. The app policy added in #6007
selects only `agentplane-app`, so it cannot help Actions.

Recommended change: mirror the narrowly selected Authentik:9000 canonical-SNI
permission for Actions before routing its issuer traffic through the Gateway
Service. Repeat positive/wrong-SNI/plaintext controls from Actions. Adding that
permission alone does not change its current DNS path or fix node-IP TLS.

### B. Mitmproxy's Docker-CI FQDN rule grants no node:2376 access

Source: `cluster/k8s/agents/mitmproxy/cnp-cloud-api-egress.yaml:120`.
The existing comment labels this suspected and unverified. It is now confirmed.

The live proxy resolved `docker-ci.allegedly.works` to the five public OVH node
IPs. Cilium on its node maps `147.135.37.175/32` to `reserved:remote-node`.
The proxy's rule uses `toFQDNs` on TCP 2376, while its cluster-entity rule grants
only 80, 443, 8000, 8080, and 11434. There is no overlapping standard KNP egress
allowance selecting this Pod.

At 04:08:25 UTC, a credential-free TCP connect from
`agents-mitmproxy/mitmproxy-5fd5cd98f6-qd527` timed out. Cilium monitor reported:

```text
Policy denied; bpf_lxc.c:1651
identity 58778 -> remote-node
10.244.2.242:40074 -> 147.135.37.175:2376 tcp SYN
```

No Docker API request, mTLS credential, or bearer was used. This proves the
network permission is ineffective, not that a current consumer exercises it.
Do not simply broaden `toEntities: cluster`: first establish the intended
Docker-CI public exposure and whether this older proxy consumer is retained.

### C. Docker-CI TLSRoute has no matching Gateway listener

Related routing finding, not a Cilium policy denial.
`cluster/k8s/docker-ci/tlsroute.yaml:10` references `docker-ci-tls`, absent from
the current cluster Gateway. Live status has `Accepted=False`, reason
`NotAllowedByListeners`, message `No Listener with matching Protocol. Allowed
protocols: [TLS]`, while backend references resolve successfully.

The condition's transition timestamp is 03:17:03 UTC. #5999 removed only
`kube-api-passthrough`; its diff did not remove a `docker-ci-tls` listener.
No claim is made about when the Docker route last worked.

Fixing B alone will therefore not establish a working public Docker path.
Determine whether to retire the unused route/permission or restore a reviewed
TLS-passthrough exposure with a non-conflicting listener. Do not restore the
removed mixed-protocol port-443 overlap.

## Coverage and checks without additional findings

- Enumerated all 45 Cilium policy resources under `cluster/k8s`; all 40 live
  policies (36 CNP, four CCNP) have repository counterparts. The five absent
  resources are in `cluster/k8s/x`: Google Workspace MCP, dispatcher,
  workers-LiteLLM, and two worker-zone policies. They were statically reviewed
  but have no runtime verification.
- Included all 25 live standard Kubernetes NetworkPolicies when checking
  overlapping allowances. No standard policy selected a currently present Pod
  in the same direction as a Cilium policy in this snapshot.
- Checked policy selectors against Pod and namespace labels. Empty matches
  corresponded to absent/ephemeral workloads (CPAP jobs, runtime harness runners,
  and claude-sandbox), and the old Props Loki consumer rule; no claim that an
  absent Pod alone indicates a defective selector.
- Resolved numeric and named Service target ports against selected Pods: 97
  cases where a Service frontend port was allowed, no uncovered translated
  target-port mismatch. This is a candidate check, not a complete network-policy
  evaluator or proof that every required application dependency is allowed.
- Live HTTPRoute backends selected by Cilium ingress policies had an ingress
  entity allowance. This does not establish successful application-level auth.
- No live Cilium policy reported a non-True `Valid` condition.
- Grafana's `app: grafana` selector still matches live Pods. Loki clients are
  configured against read/write:3100; the Gateway's translated 8080 is allowed
  for its internal canary/Loki clients. No evidence warrants a Loki policy edit.
- `**.cluster.local` is supported by the deployed Cilium match-pattern code;
  treating it as an unsupported shell-style glob would be a false finding.

## Existing security tradeoffs, not new regressions

The sandbox `force-proxy` policies intentionally allow broad cluster traffic,
and kube-dns requests are not name-filtered there. Proxy DNS allowlists do not
constrain queries made by those sandbox Pods. The repository already records
both these gaps and the Docker-daemon relay risk in
`cluster/validation/test_egress_allowlists.py` and the sandbox CCNP comments.
Public-coder's broad proxy internet access is also an explicit waiver.
Changing these is a security-boundary redesign, not a mechanical policy fix.

## Validation and evidence limits

`bbr test //cluster/validation:test_egress_allowlists
//cluster/validation:test_cluster_integration`: both PASSED from cache.

- BuildBuddy: <https://app.buildbuddy.io/invocation/691dcda5-c47c-45b1-ab42-aec134cff3b2>
- Runner: <https://app.buildbuddy.io/invocation/14e4714e-8d43-4df5-aa12-14e1ecf80b75>

The existing validation does not cover the demonstrated runtime failures or
the missing TLSRoute listener. No full application acceptance was run.
Future validation should include Gateway client/backend policy contracts and
TLSRoute parent-listener compatibility, without pretending static checks prove
datapath behavior.

Sanitized local inventories: `/tmp/cilium_policy_audit_{live,services,pods,routes,
knp,namespaces}.json`. Pod inventory contains labels, network placement and
container ports only, no environments or Secret data. Temporary diagnostic
scripts under `/tmp/cilium_policy_audit_*.py` read these inventories; they are
not repository tests and were not used as substitutes for Bazel validation.
