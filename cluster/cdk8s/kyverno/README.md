# Kyverno policy tests

These tests synthesize each policy from its cdk8s constructor and run the real
Kyverno CLI against focused resource fixtures. They do not load the Flux delivery
manifests.

## Layout

| File               | Contents                                            |
| ------------------ | --------------------------------------------------- |
| `apply.py`         | Runs `kyverno apply` and parses its result.         |
| `apply_inputs.py`  | Looks up fixtures and synthesizes one policy chart. |
| `test_<policy>.py` | One module and Bazel target per policy.             |
| `testdata/`        | Input resources for the policy behavior cases.      |

## Coverage

Tested: `default-disable-service-links`, `require-gitops` (the request user comes from
`--userinfo`), `require-secret-store-conditions` and the janitor `NamespacedDeletingPolicy`
(`janitor.py`, which `test_janitor.py` builds inline and applies to Pods of different ages).
Offline `kyverno apply` cannot map a custom resource's kind, so `require-secret-store-conditions`
exercises its CEL validations in JSON mode and checks admission and background matching on the
original policy.
