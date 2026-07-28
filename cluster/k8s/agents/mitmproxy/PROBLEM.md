# Mitmproxy Kyverno Injection vs. Pod Patch Operations

## Problem

The `inject-mitmproxy` Kyverno ClusterPolicy mutates pods at creation time in
`claude-sandbox` and `openclaw-gateway` namespaces. It uses
JSON Patch (`patchesJson6902`) to append volumes, env vars, and volume mounts.

`openclaw-sandbox` is deliberately excluded: OpenShell's supervisor proxy owns
egress policy and credential injection there. Injecting this generic proxy into
an OpenShell sandbox prevents the supervisor from observing the original
request and replacing its credential placeholders.

This causes **duplicate entry errors** when any subsequent `PATCH` or `UPDATE`
operation is sent to the API server for an already-admitted pod. Kyverno
re-evaluates the policy on the update, tries to append the same injections again,
and the API server rejects the request because the volume name / mount path / env
var already exists.

## Affected Operations

| Operation                     | Example                                                                                | Result                                                                               |
| ----------------------------- | -------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| Remove pod finalizers         | `kubectl patch pod X --type=json -p '[{"op":"remove","path":"/metadata/finalizers"}]'` | **Rejected** — Kyverno re-mutates, duplicate volume/env                              |
| Update pod labels/annotations | `kubectl label pod X key=value`                                                        | **Rejected** (same reason)                                                           |
| `kubectl replace`             | Full pod replacement                                                                   | **Rejected** (same reason)                                                           |
| Force delete                  | `kubectl delete pod X --force --grace-period=0`                                        | Works (uses DELETE, not PATCH) but pod may stay stuck if finalizers can't be removed |

## Why It Happens

1. Pod is created → Kyverno mutates (appends proxy volume/env/mount) → pod admitted
2. Later, `kubectl patch pod X -p '{...finalizers...}'` is issued
3. Kyverno intercepts the UPDATE, evaluates `inject-mitmproxy` again
4. The JSON Patch `add /spec/volumes/-` appends a second `mitmproxy-ca-cert` volume
5. API server validation rejects: duplicate volume name

The root cause is that `patchesJson6902` with `add /-` always appends — it has no
idempotency check ("skip if already present").

## Workarounds

- **Force delete** (`kubectl delete --force --grace-period=0`) uses DELETE, not PATCH
- **Finalize subresource** (`PUT .../pods/{name}/finalize`) bypasses Kyverno but
  requires crafting the full pod JSON and may fail on some API server versions
- **Exclude the pod** by labeling it before patching (if the policy had an
  objectSelector exclusion — it currently does not)

## Possible Fixes

1. **Add a precondition to the Kyverno policy**: Check if the volume/env is already
   present before injecting. Kyverno's `preconditions` can do this but the logic
   is verbose for array membership checks.

2. **Use `mutateExistingOnPolicyUpdate: false`** (already set) — this prevents
   re-mutation when the _policy_ changes, but does NOT prevent re-mutation when
   the _pod_ is updated.

3. **Add an objectSelector exclusion**: Allow pods with a specific label
   (e.g. `skip-mitmproxy-injection: "true"`) to opt out. This lets operators
   patch/delete finalizers by first labeling the pod.

4. **Switch to Kyverno's `foreach` with `anyPattern`** and use conditional logic
   to skip already-injected pods — complex and fragile.

5. **Accept the tradeoff**: Pod patch operations are rare in sandbox namespaces.
   Force-delete works for cleanup. The injection is intentionally "always on" for
   turnkey proxy support.

## Current Impact

Encountered during CNPG cluster cleanup in `claude-sandbox`. Orphaned initdb pods
from deleted CNPG clusters have `batch.kubernetes.io/job-tracking` finalizers that
can't be removed via PATCH. The pods remain in Failed/Completed state indefinitely
as a result.
