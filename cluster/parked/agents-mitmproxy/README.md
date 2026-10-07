# Parked Claude sandbox mitmproxy

The generator remains in `cluster/cdk8s/mitmproxy.py`, with its CA, trust bundle, proxy and policies rendered here for
revival. The former Authentik blueprint is `agents-mitmproxy-sso.yaml.disabled`, not a Kubernetes manifest or active
blueprint.

The original Flux owner used `deletionPolicy: Orphan`. Retirement therefore first reconciled that same
`agents-mitmproxy` owner against an empty directory with pruning enabled, instead of deleting the owner or merely
suspending it. Read-only live checks on 2026-10-02 at 21:57 UTC confirmed Ready on the #8806 merge (`93ee322b`), an
empty inventory, and no proxy namespace or Pods. The verified-empty retirement owner and directory are now removed;
nothing deploys this archived tree.

`claude-sandbox` itself remains active **only as an identity/credential home** for external sessions: its shared RBAC
and credential delivery are not removed. The same live checks confirmed no Claude Pods, a zero Pod quota, the deny-all
egress policy, and all four ExternalSecrets Ready/SecretSynced. The former proxy-owned clusterwide allow policy was part
of the pruned inventory. Quotas prevent creation but do not evict existing Pods.

The unused `inject-mitmproxy` Kyverno policy, public traffic-viewer route and Authentik outpost attachment are removed.
An active Authentik tombstone deletes the old application/provider; the generator for proxy injection is preserved.
Haku's distinct proxy, injection policy, CI, and namespace are unchanged.

To revive, restore this generator's active Flux call and output path, re-enable `inject_mitmproxy_chart` in the Kyverno
chart list, restore the route/outpost and replace the Authentik tombstone with the archived blueprint. Restore Claude's
Pod quota (previously 50) and remove `parked-compute-egress` only once the proxy fence is installed and verified.
Regenerate and validate the resulting manifests.
