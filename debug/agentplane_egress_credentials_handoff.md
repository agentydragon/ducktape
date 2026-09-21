# Agentplane egress credential namespaces

Each environment's existing Flux owner owns its proxy and a dedicated credentials
Namespace. `agentplane-staging-egress-credentials` contains GitHub and Forgejo
credentials; `agentplane-testing-egress-credentials` contains GitHub only. The proxies
keep namespace-wide Secret watches without gaining access to application Secrets.

ESO copies the GitHub token from `ducktape-flux/github-agentydragon-agent` into both
namespaces. Staging alone has an exact-name, get-only grant to the Terraform-owned
`haku-sandbox/haku-forgejo-git` and copies only its password. Neither canonical source
is rotated or transferred. GitHub acceptance remains in testing.

## Sequence

1. Merge credential preparation. New namespaces, referent ServiceAccounts,
   ExternalSecrets and source grants reconcile while both proxies still consume the
   old namespace. Confirm all three ExternalSecrets and the Forgejo store are Ready,
   and generated target Secrets have ESO owner references. Do not print Secret data.
2. Merge the proxy switch only after step 1. Wait for all staging and testing proxy
   replicas to use their respective new namespaces and become Ready. Verify GitHub
   authenticated egress in both environments and Forgejo authenticated egress in staging.
   Testing no longer emits the Forgejo credential or policy; its GitHub policy,
   public-coder preset and GitHub acceptance test stay intact. Keep runtime identifiers
   local. Check that testing cannot read the staging credential namespace or canonical
   Forgejo Secret; old shared-namespace access is removed in step 3.
3. After both rollouts, retire the old namespace and credentials Flux owners, remove
   the old GitHub source grant/store namespace condition, and remove only the old
   credentials destination from the Forgejo source's Reflector annotations. The old
   namespace contains derived credential copies and RBAC only: no workloads, PVCs,
   or authored data. Recheck that before allowing its deletion. Namespace deletion
   intentionally removes the old copies and both old proxy read bindings; canonical
   Secrets and new copies remain. Other Reflector consumers remain configured.
4. Verify the old namespace and both Flux owners are gone. Check testing can read
   its own GitHub copy but cannot get/list/watch staging's credential Secrets or
   read the canonical Forgejo Secret, and staging cannot read testing's copies.
   Confirm ESO refresh, ready Service endpoints and the application paths again.

These are staged credential distribution and deployment changes, not an in-place
Reflector-to-ESO adoption. The two controllers never manage the same destination.

## Recovery

Before retirement, either proxy can be switched back to the still-existing old
namespace. After retirement, restore distribution before switching back. Removing
resources from either environment owner while it has pruning enabled deletes them
regardless of its `Orphan` owner-deletion policy; retain the new resources until
no proxy consumes them.
