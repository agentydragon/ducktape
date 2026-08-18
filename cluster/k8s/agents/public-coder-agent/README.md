# public-coder-agent

A second OpenClaw agent at <https://public-coder-agent.allegedly.works>, separate
from the personal agent at `openclaw.allegedly.works`. Its job is opening pull
requests against **public** repositories as `agentydragon-agent`, which has its
own GitHub account and pushes to its own forks.

## Layers

| Directory    | Contents                                                             |
| ------------ | -------------------------------------------------------------------- |
| `namespace/` | Namespace only                                                       |
| `proxy/`     | Interception CA, trust bundle, iron-proxy, and the FQDN allowlist    |
| `app/`       | OpenClaw Deployment, config, state PVC, credentials, NetworkPolicies |

The agent also connects to Matrix as `@public-coder-agent:allegedly.works`.
Matrix is an official plugin baked into the derivative OpenClaw gateway's
trusted bundled-extension tree; loading it from an arbitrary config path would
deny the state-store capability it needs before sync. The initial policy is
deliberately DM-only and allowlisted to
`@agentydragon:allegedly.works`; add explicit room policy/config before using
it in group rooms.

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

The GitHub credential stays in the proxy pod. The agent sees only
`proxy-github-placeholder`; iron-proxy replaces it in the authentication header
and only on scoped GitHub hosts.

Haku Console privileged calls use the same mediated shape. Terraform generates a dedicated
`public-coder-agent` static-Agent bearer and delivers it only to Haku Console and iron-proxy. The
OpenClaw container sees `proxy-haku-console-placeholder`, which is replaced only in the
`Authorization` header for `haku.allegedly.works`. Haku Console assigns this Agent the explicit
`no_auto_approval` policy: every tool call becomes an operator-reviewed request, including the
cluster-admin-backed kubectl passthrough surface, and the Agent bearer cannot approve requests.

BuildBuddy is the deliberate exception. The agent receives the real shared API
key from the reflected `buildbuddy-api-key` Secret. A local Bazel client or the
local `bb remote` control channel could each use proxy substitution alone, but
their combination cannot: `bb remote` embeds the key in the Bazel command sent
to its hosted runner, and that nested process uses BuildBuddy BES, cache, and
RBE outside this proxy. A placeholder therefore fails on the runner. This is an
accepted credential-exposure tradeoff for remote Bazel plus remote execution;
revisit it if the agent later uses only one of those layers.

The Matrix bot password is generated and retained by the existing Matrix user
provisioner. It is stored in the Matrix namespace as a SOPS-managed Secret and
reflected into `public-coder-agent`, where only iron-proxy consumes it. The
OpenClaw container sends `proxy-matrix-password-placeholder` in its password
login body; iron-proxy replaces it only on Synapse's login endpoint. The
provisioner sets the password only when creating the account, so reconciliation
does not revoke the bot's cached access token. The Matrix channel names the
same iron-proxy Service explicitly in `channels.matrix.proxy`; the plugin's
guarded fetch path uses that per-channel dispatcher rather than Node's generic
proxy environment handling.

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
- **Temporary commit-built iron-proxy image.** <../../../images/iron-proxy/>
  and `.github/workflows/iron-proxy-image.yml` build upstream commit `c90f4fe`
  into the private Forgejo registry because it adds the HTTP/2/gRPC MITM support
  BuildBuddy needs but has not been released yet. Flux rolls the proxy to that
  image after the first push. Return to the official image and delete this build
  path once iron-proxy v0.50.0 ships stable — as of 2026-08-09 `c90f4fe` is
  v0.49.0 + 1 commit and rides in `v0.50.0-rc.2`, but no non-RC tag has it. The
  image is shared with
  `haku-claude-oauth-proxy` and `haku-openclaw-spike-proxy`, so it is not owned
  here — it was named `public-coder-iron-proxy` until 2026-08-09 only because
  this was its first consumer.
- **`gateway.bind: all`**, unlike the loopback-bound lab rig, because the outpost
  reaches this pod over the cluster network. What makes that safe is
  `app/networkpolicy-ingress.yaml`, which admits only the outpost's pods —
  without it any pod could forge `x-authentik-username`.

## Known gaps

- **Matrix token auth.** TODO: replace the password-body swap with a
  proxy-held Matrix access token once the provisioner can mint and rotate one
  without revoking Haku Console's independent session. For v1, the pinned
  iron-proxy's `match_body` replacement keeps the password out of OpenClaw.
- The egress NetworkPolicy's DNS rule has no destination selector, so port 53
  reaches anywhere and DNS tunnelling is not prevented. Narrowing to kube-dns is
  the obvious tightening; it is left as a follow-up so the first deployment
  matches what was validated in the lab.
- With `sandbox.mode: "off"` there is no isolation _inside_ the boundary: the
  agent runs as the harness, with the GitHub token in its environment. That is
  the accepted cost of not using OpenShell — see
  <../../../../plans/personal_agents/findings/openshell.md> F1 for why OpenShell was not
  used.
- **PVC capacity enforcement.** `local-path-ovh-hdd` does not enforce requested
  PVC sizes, so workspace growth can consume the worker disk beyond its claim.
  Before treating this as durable agent storage, evaluate extending the
  OpenEBS LVM provisioner to OVH workers and migrating this claim to a
  size-enforcing LVM-backed StorageClass (or another quota-enforcing design).
- **Runtime image closure.** Profile and reduce the OpenClaw image before its
  next substantial expansion. The 2026-08-17 image is 2.61 GiB compressed
  across 99 layers. It intentionally includes the gateway and Matrix plugin,
  but also the broad `devtools` and git-hook closures (Bazel/`bbr`, Ansible,
  AWS CLI, Checkov, Rust tooling, and formatter/pre-commit tooling). Identify
  the actual runtime-required subset and move the rest to the dedicated
  devbox or on-demand tooling without breaking agent workflows.
