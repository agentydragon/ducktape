# Kyverno policy tests

Tests for the ClusterPolicies in <../../k8s/kyverno/policies/> (and the zone
injector in <../../k8s/x/haku/zones/policies/>), driven by the real `kyverno` CLI
from the multitool lockfile — so they exercise the policy engine itself, not a
model of it.

## Layout

| File               | Contents                                                             |
| ------------------ | -------------------------------------------------------------------- |
| `apply.py`         | `apply_policy()` wrapping `kyverno apply`, returning a parsed result |
| `paths.py`         | runfiles lookups: `manifest()` for testdata, `policy()` by file name |
| `test_<policy>.py` | one module and one `py_test` target per policy                       |
| `testdata/`        | input manifests, prefixed by the policy that consumes them           |
| `__snapshots__/`   | syrupy snapshots, one `.ambr` per test module                        |

**One target per policy.** A failure names the policy it belongs to, and a
single policy can be run alone. `test_proxy_injection` is the deliberate
exception: it asserts a contract every proxy-injection policy must satisfy and
is parameterized over them, so a new injector is covered without editing it.

## Coverage

Tested: `default-vpa-requests-only`, `default-disable-service-links`, `inject-mitmproxy`,
`inject-haku-egress-proxy`, `require-secret-store-conditions`,
`restrict-agent-gateway-routes`.

Untested, and why:

- `require-gitops` and `restrict-agent-kustomization-patch` match on
  `request.userInfo`, which plain `kyverno apply` does not supply. They need
  `--userinfo` or a mock admission context.
- `default-revision-history-limit` has no test yet; it is the same shape as
  `default-vpa-requests-only` (mutate, add-if-absent), so
  `test_default_vpa_requests_only.py` is the template to copy.

## Gotchas

- **Do not name a shared helper `test*`.** pytest's default
  `python_functions = test*` collects any imported callable starting with
  "test", so a helper named `testdata` is picked up as a test and fails looking
  for a fixture. Hence `manifest()`.
- **Syrupy names the `.ambr` after the test module**, so splitting or renaming a
  snapshot-backed module means renaming its snapshot file in the same change.
- **New testdata must match its target's `glob`.** The globs are prefix-scoped
  per target (`testdata/pod_*.yaml`, `testdata/namespace_vpa_*.yaml`, …); a file
  outside the prefix is not in runfiles and the target fails at analysis time.
