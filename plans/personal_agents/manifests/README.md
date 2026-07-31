# Confined OpenClaw — the configuration that passes the hard requirements

The setup validated in `agent-lab` as of 2026-07-29. Reproduced verbatim from
the lab so it can be reviewed, and adopted or discarded, without re-deriving it.
Results and caveats: <../lab_notes.md>.

**Deliberately not under `cluster/k8s/`.** These are a proposal, not something
Flux should apply. Promoting them means choosing a namespace, deciding where the
GitHub PAT should live, and re-checking the open items below.

## Shape

One container behind one boundary — the "whole harness sandboxed" topology
(B2/NemoClaw-style), which `requirements.md` records as an acceptable outcome:

- `openclaw-confined.yaml` — OpenClaw as a **plain Deployment**, not via the
  operator, with `sandbox.mode: "off"` so commands run in the harness container.
  State lives on a PVC. All egress is forced through the proxy by a
  NetworkPolicy in the same file.
- `egress-allowlist-proxy.yaml` — mitmproxy with a CONNECT-host allowlist addon.
  It resolves hostnames itself, so a client cannot smuggle an allowed name to an
  arbitrary address. The addon is `allowlist.py`;
  `credential_injection_addon.py` is the alternative that also attaches the
  GitHub token (<credential_injection.md>). Swap which one the `lab-proxy-addon`
  generator points at — they populate the same mounted path.
- `openclaw.json` — the gateway config, planted into the state dir by an
  init container.

Every config blob is a real file wired in by <kustomization.yaml> via
`configMapGenerator` — `allowlist.py`, `iron.yaml`, `envoy.yaml` — rather than
embedded in a YAML string, so they stay lintable and diffable.

`iron-proxy-lab.yaml` (config: `iron.yaml`) is an **alternative to the two files
above**, not an addition: the off-the-shelf proxy in place of mitmproxy plus our addon. Adopt one
or the other. It is the configuration that actually ran in F16 — a real OpenClaw
holding only a placeholder, which opened a pull request end to end — plus the
system-trust-store fix from F17, and it carries the reasoning for each choice
inline. Its known gap is fail-open on a missing secret.

## Why not the operator

The OpenClaw operator's generated NetworkPolicy always contains an egress rule
allowing **443/TCP to any destination**, and `allowedEgressCIDRs` appends rather
than replaces it. Egress confinement in that shape is therefore advisory —
whatever honours `HTTP_PROXY` is confined, and anything that ignores it is not.
Owning the NetworkPolicy is the whole reason for the plain Deployment.

## Results

| Criterion                        | Result                                                         |
| -------------------------------- | -------------------------------------------------------------- |
| S1 stood up                      | pass                                                           |
| S3 memory across sessions        | pass — recalled in a fresh session, and survives a pod restart |
| S4 no arbitrary Internet         | pass — direct egress fails even to allowed hosts               |
| S5 whole harness confined (want) | pass                                                           |
| S2 PR end to end                 | see lab notes — needs the GitHub PAT wired in                  |

## Before promoting this, settle

- **Where the GitHub PAT lives.** The lab pulls `agentydragon-agent`'s PAT into
  `agent-lab` through the same `ClusterSecretStore` the OpenShell provider uses.
  That widens where that credential exists. OpenClaw's cloud-worker model keeps
  forge credentials off the execution box entirely (findings.md, C8) and is the
  better long-term answer.
- **The allowlist is currently GitHub-only.** Anything else the agent needs —
  package registries, docs — has to be added deliberately, which is the point,
  but it means the list is a maintained artifact.
- **The proxy CA is generated at first start** and copied into a ConfigMap by
  hand. A promoted version should mint it from cert-manager instead.
- **No sandbox _within_ the boundary.** Everything the agent runs shares the
  harness container, so this trades the agent/harness split away for a boundary
  that actually holds. That is the acknowledged cost of this topology.
