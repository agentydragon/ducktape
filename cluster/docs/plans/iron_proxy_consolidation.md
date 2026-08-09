# Consolidating the iron-proxy deployments

Probe note, 2026-08-09. Question: the cluster runs three iron-proxy Deployments
and would gain a fourth if `haku-sandbox` moves off mitmproxy — should they
share one definition instead of being copies?

## What the copies actually share

Measured by diffing the three with comments stripped.

`haku-claude-oauth-proxy` (73 lines) and `haku-openclaw-spike-proxy` (106 lines)
are **byte-identical except for five things**:

| Axis          | claude                        | openclaw spike                     |
| ------------- | ----------------------------- | ---------------------------------- |
| Name / labels | `haku-claude-oauth-proxy`     | `haku-openclaw-spike-proxy`        |
| `description` | one sentence                  | one sentence                       |
| `env` secrets | 1 (`CLAUDE_CODE_OAUTH_TOKEN`) | 5 (adds Forgejo, console, GH, JWT) |
| Listener port | 8180                          | 8181                               |
| Config CM     | `…-proxy-config`              | `…-proxy-config`                   |

Everything else — `imagePullSecrets`, `automountServiceAccountToken: false`, the
pod and container `securityContext`, the image and its `$imagepolicy` marker,
`args`, `resources`, `volumeMounts`, the shared `haku-egress-proxy-ca` volume —
is the same text twice.

`public-coder-agent-proxy` (103 lines) is a weaker fit: different namespace,
its own CA secret (`public-coder-agent-proxy-ca`, not the shared haku CA), and
port 8080. Two of three is the realistic sharing boundary.

## The finding that matters more than the duplication

**`public-coder-agent-proxy` has none of the hardening its two siblings have.**
Verified against the live pod, not just the manifest:

```text
spec.securityContext:            {}
containers[0].securityContext:   (absent)
serviceAccountName:              default   # token automounted
```

The manifest sets no `runAsNonRoot`, no `runAsUser`, no `seccompProfile`, no
`allowPrivilegeEscalation: false`, no `capabilities: drop: ["ALL"]`, and no
`automountServiceAccountToken: false`. The namespace carries no
`pod-security.kubernetes.io/*` labels, so the cluster-default **baseline** is
all that applies — and baseline requires none of those. The container runs as
root with a mounted default ServiceAccount token.

This is the **most** credential-dense of the three: it holds the real GitHub
PAT, a Haku Console bearer, and a Kubernetes reader token, and its egress is
deliberately unrestricted. It is the one that should be hardest, and it is the
softest.

Nothing about the deliberate egress waiver implies a pod-security waiver — the
waiver is documented in `cnp-egress.yaml` as being about destinations. This
reads as drift from copy-paste divergence, not a decision.

**Recommended before any refactor**: add the five fields to
`public-coder-agent/proxy/deployment.yaml`, matching the haku siblings. Low
risk — the same image already runs as uid 65532 in both other deployments, and
port 8080 needs no privilege. It restarts one proxy pod.

## Verdict on the component itself

Worth doing, but for drift-prevention rather than line count — the finding above
is exactly the failure mode, and `//cluster/validation:test_egress_allowlists`
already exists because these fences drifted once before.

Deduplicating the two haku proxies saves perhaps 80 lines, which alone would not
justify the mechanism. What justifies it is that the `haku-sandbox` migration
adds a fourth copy, and a copy is where hardening goes missing.

**Shape**: a kustomize component under `cluster/k8s/agents/haku-egress-proxy/`
holding the invariant Deployment, with per-instance patches supplying name,
port, config-map name, and the extra `env` entries. Kustomize components cannot
parameterize a name or port directly, so each instance still needs a small
patch — the win is that the security and image stanzas exist once.

**Open question the probe did not settle**: whether the patch-per-instance
verbosity leaves enough benefit to be worth the indirection, given the cluster
validator and reviewers both read the rendered output. Decide that by writing
the component for the two haku proxies and comparing the rendered result against
today's files; abandon it if the overlays approach the size of what they replace.

## Next step

1. Harden `public-coder-agent-proxy` (independent of everything else here).
2. Only then decide the component, and fold it into the `haku-sandbox`
   migration rather than doing it standalone — that migration is what creates
   the fourth copy.
