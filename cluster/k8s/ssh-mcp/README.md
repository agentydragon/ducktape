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

## Deployment prerequisites

- Publish the renamed `ssh-mcp` image through the registered CI target and let its
  Flux image policy replace the initial placeholder with a published tag before
  reconciling the backend. An image published under a different repository name
  does not establish availability at the new name.

- Capture `rugged`'s SSH host key and add it to `known_hosts` once the host is
  reachable — it's a roaming laptop and was offline at provisioning time, so only
  `wyrm2`'s host key (verified against the operator's own trusted `known_hosts`) is
  populated so far. Empty host trust and missing identity files do not authorize SSH;
  host verification must remain strict, so the `rugged` targets stay unavailable
  until its host key is captured and reviewed.

Rendered configuration and policy tests prove wiring, not live image availability,
secret reconciliation, network reachability, or successful SSH execution.
`ssh-mcp-keys` (four per-`(host,user)` ed25519 keys for `{wyrm2,rugged} ×
{agentydragon,root}`, SOPS-encrypted in `secrets/keys.sops.yaml`), the public halves
in each host's NixOS `authorizedKeys`, and the egress `NetworkPolicy` (pinned to the
hosts' Nebula addresses) are provisioned; live SSH execution against each target is
still unverified until the hosts have `nixos-rebuild switch`ed and the backend image
is published.
