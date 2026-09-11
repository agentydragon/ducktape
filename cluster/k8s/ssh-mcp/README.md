# Shared SSH MCP backend

The standalone backend serves `haku-console` and `agentplane-staging` only. Each
consumer owns its approval policy; the backend owns SSH transport and private keys.
`agentplane-testing` has neither an SSH binding nor the backend bearer.

## Credentials and reconciliation

`secrets/bearer-eso.yaml` invokes one ESO Password generator in `ssh-mcp`.
`CreatedOnce` preserves the generated bearer across ordinary reconciliations.
Reflector distributes that Secret to exactly `haku-console` and
`agentplane-staging`; neither consumer invokes a generator. All three deployments
reload when their bearer Secret changes. Deleting the source Secret recreates the
bearer and triggers an asynchronous mirror/reload rollout, so rotation can briefly
interrupt calls.

The secrets Flux Kustomization depends on the backend namespace, ESO configuration,
Reflector, and Forgejo image credentials. Both consumers depend on that secrets
layer, not the backend Deployment's readiness. The backend namespace has its own
non-pruning Flux owner.

## Targets

`ssh-mcp-keys` holds four per-`(host,user)` ed25519 keys for `{wyrm2,rugged} ×
{agentydragon,root}`, SOPS-encrypted in `secrets/keys.sops.yaml`, with the public halves
in each host's NixOS `authorizedKeys`.

Both `wyrm2` targets execute end to end. The `rugged` targets do not, and will not until
that host rejoins the cluster: its SSH host key was never captured and its Nebula address
does not answer. They stay listed and fail closed — the designed behaviour for a target
whose key or host trust is absent. Host verification stays strict, so capturing that key
is a prerequisite for those two targets, not a formality.

`public-coder-devbox × {root,coder}` is a second pair of targets, keyed in a **separate**
Secret, `ssh-mcp-keys-public-coder-devbox` (`secrets/keys-public-coder-devbox.sops.yaml`),
mounted as a second volume alongside `ssh-mcp-keys`. Deviation from the pattern above:
whoever adds a target normally appends to `keys.sops.yaml` with `sops <file>`, which needs
the cluster decryption identity; an agent session that can only encrypt (not decrypt) that
file cannot safely do that without risking the working `wyrm2`/`rugged` keys it already
holds, so it mints a new Secret instead. Reaching the devbox is a normal in-cluster
`toEndpoints`/Service DNS path (`cluster/k8s/agents/public-coder-agent/devbox/`), not a
Nebula hostAlias — it is an ordinary pod (a KubeVirt VM), not a cluster node.

`atlas × {root,agentydragon}` is a third pair of targets, following the same
separate-Secret pattern as the devbox pair: `ssh-mcp-keys-atlas`
(`secrets/keys-atlas.sops.yaml`). `atlas` is a Nebula mesh peer like `wyrm2`/`rugged`
(reached over the same `hostAliases` pinning), but unlike them it is not a registered
Kubernetes node (`nebula-mesh.json`: `role: "non-k8s"`), so it needs its own network
selector shape — see below. Like `rugged`, its SSH host key has not yet been captured, so
the target is wired but stays unverified/fail-closed until that key is pinned into
`known_hosts`.

## Network path

Three Pod-specific properties are load-bearing, each commented at its own declaration:
the egress policy selects real cluster nodes by entity rather than by CIDR
(`networkpolicy.yaml`), non-node Pod targets by `toEndpoints`, and the non-k8s Nebula peer
`atlas` by `toCIDR`; target hostnames are pinned with `hostAliases` (`deployment.yaml`).

`wyrm2`/`rugged` are cluster nodes reached over Nebula, so neither a CIDR selector nor
cluster DNS resolves them the way a host on the mesh does — hence entities + hostAliases.
`public-coder-devbox` is an ordinary pod, so it gets a normal cross-namespace
`toEndpoints` rule instead. `atlas` is a Nebula peer but not a cluster node, so its address
never gets the host/remote-node identity the entities rule relies on; a plain `toCIDR` rule
naming its Nebula IP is the selector for a genuinely external destination. Unlike the
entities rule (proven by the working `wyrm2` targets), the `toCIDR` rule for `atlas` is
**unverified against the live cluster** — it has not yet been exercised because `atlas`'s
host key is not yet captured (see above), so no connection through it has actually been
attempted.

Rendered configuration and policy tests prove wiring only — not secret reconciliation,
network reachability, or successful SSH execution.
