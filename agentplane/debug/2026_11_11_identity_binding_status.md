# Identity binding refusal status

Live staging inventory checked 2026-11-12 UTC.

`haku-ui` and `haku-ci-egress` bind existing Identities. Their selected PolicySets contain
only egress and Action rules. Both report `EgressReady=True`, but also
`KubernetesReady=False`, reason `SubjectNotServiceAccount`. The frontend's
`bindings/state.ts` treats any false condition as a refusal.

`policy_controller/controller.go:expand` returns this refusal for every Identity subject,
before inspecting the selected rules. That branch landed in #15076. Moving Haku's standing
credentials and bindings to Identities in #15450 exposed it. The correction reports zero
native bindings applied when no Kubernetes rules are selected; actual Kubernetes rules
on an Identity still report refusal. Missing PolicySets still report unavailable.

## Other migration state

- No Sandbox exists in any namespace. The only live Identities are `haku-ui` and
  `haku-ci-egress`; the four standing WorkloadCredentials name them.
- `agentplane-staging-k8s-accounts/{public-coder,finance-agent}` both exist. Their
  ServiceAccountGrants report respectively 66 and 69 native bindings applied. The
  corresponding `k8s-account-*` EgressCredentials and use PolicySets exist.
- An authorization check as the finance account allows pod inventory across namespaces
  and node reads, and denies reading Secrets in `kube-system`. Its additional role sets
  name the Coinbase reader, spend-policy configuration reader and Plaid diagnostics reader.
- #15712 supplied the one-time Sandbox data migration; #15720 removed it after recording
  that `public-coder-2` had been migrated. The controller switch #15644 remains open;
  current `devel` still creates Sandbox ServiceAccounts and bindings.
- There is no static Identity named `public-coder` or `finance-agent` in the staging
  generator. Its gated standing roster includes the inactive `public-coder-openclaw`.
  Enabling that catalog does not create a standing Identity for either launch preset.

The status correction does not enable the remaining identity cutover or create new
standing identities. After its controller image rolls out, re-read both Haku bindings:
expect `KubernetesReady=True`, `Applied`, zero native bindings, and the existing egress
conditions to remain true. Reverting the correction restores the misleading status;
binding authorization and account grants are unchanged.

A read-only aggregate of `egress.egress_decision` over the preceding 24 hours found 15,191
allowed requests for `haku-ci-egress`, latest at `2026-11-12T06:23:42Z`, plus 16 `no-rule`
denials, latest at `2026-11-11T08:25:25Z`. There were no `haku-ui` decisions in that window,
so its ready conditions are not end-to-end traffic evidence.
