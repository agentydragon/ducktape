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

## Network path

Two Pod-specific properties are load-bearing, each commented at its own declaration: the
egress policy selects nodes by entity rather than by CIDR (`networkpolicy.yaml`), and the
target hostnames are pinned with `hostAliases` (`deployment.yaml`). Both exist because the
targets are cluster nodes reached over Nebula, so neither a CIDR selector nor cluster DNS
resolves them the way a host on the mesh does.

Rendered configuration and policy tests prove wiring only — not secret reconciliation,
network reachability, or successful SSH execution.
