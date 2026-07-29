# public-coder-agent

A second OpenClaw agent at <https://public-coder-agent.allegedly.works>, separate
from the personal agent at `openclaw.allegedly.works`. Its job is opening pull
requests against **public** repositories as `agentydragon-agent`, which has its
own GitHub account and pushes to its own forks.

## Layers

| Directory    | Contents                                                             |
| ------------ | -------------------------------------------------------------------- |
| `namespace/` | Namespace only                                                       |
| `proxy/`     | Interception CA, trust bundle, mitmproxy, and the FQDN allowlist     |
| `app/`       | OpenClaw Deployment, config, state PVC, credentials, NetworkPolicies |

## Egress model

Two layers, and the split matters:

1. **`app/networkpolicy-egress.yaml` is the enforcement.** The agent pod may
   reach DNS, the proxy on 8080, and in-cluster LiteLLM on 4000. Nothing else.
   The `HTTP_PROXY` variables in the Deployment are convenience — an agent that
   unsets them does not gain egress, it loses its only route out.
2. **`proxy/cnp-egress.yaml` is the allowlist.** Enforced by Cilium `toFQDNs` on
   the _proxy's_ egress, not by proxy configuration, so a CONNECT to a
   non-allowlisted host fails at the network layer. Every widening is a
   reviewable diff in that one file.

The model path never leaves the cluster: LiteLLM is reached directly, bypassing
the proxy, via `NO_PROXY`.

## Deviations worth knowing

- **Plain `Deployment`, not `OpenClawInstance`.** The operator's generated
  NetworkPolicy always contains an egress rule for 443/TCP with no destination
  selector, and `spec.security.networkPolicy` exposes only additive fields
  (`additionalEgress`, `allowedEgressCIDRs`) with no way to disable it. Since
  Kubernetes NetworkPolicies are unions of allows, that rule cannot be
  subtracted, and an operator-managed instance cannot be egress-confined. The
  cost is losing `autoUpdate` and the CRD ergonomics.
- **Blueprint-managed SSO provider**, against the stated preference for
  Terraform in <../../../docs/sso.md>. Every proxy provider is a blueprint and
  `embedded-outpost.yaml` owns outpost membership; a Terraform provider would
  split one object graph across two owners. Moves with the rest under issue #987.
- **`gateway.bind: all`**, unlike the loopback-bound lab rig, because the outpost
  reaches this pod over the cluster network. What makes that safe is
  `app/networkpolicy-ingress.yaml`, which admits only the outpost's pods —
  without it any pod could forge `x-authentik-username`.

## Known gaps

- The egress NetworkPolicy's DNS rule has no destination selector, so port 53
  reaches anywhere and DNS tunnelling is not prevented. Narrowing to kube-dns is
  the obvious tightening; it is left as a follow-up so the first deployment
  matches what was validated in the lab.
- With `sandbox.mode: "off"` there is no isolation _inside_ the boundary: the
  agent runs as the harness, with the GitHub token in its environment. That is
  the accepted cost of not using OpenShell — see
  <../../../../plans/personal_agents/lab_notes.md> F1 for why OpenShell was not
  used.
