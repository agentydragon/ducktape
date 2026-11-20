# Kyverno policy tests

These tests synthesize each policy from its cdk8s constructor and run the real
Kyverno CLI against focused resource fixtures. They do not load the Flux delivery
manifests.

## Layout

| File               | Contents                                            |
| ------------------ | --------------------------------------------------- |
| `apply.py`         | Runs `kyverno apply` and parses its result.         |
| `test_utils.py`    | Looks up fixtures and synthesizes one policy chart. |
| `test_<policy>.py` | One module and Bazel target per policy.             |
| `testdata/`        | Input resources for the policy behavior cases.      |

## Coverage

Tested: `default-disable-service-links`, `require-gitops` (the request user comes from
`--userinfo`) and `require-secret-store-conditions`. Offline `kyverno apply` cannot map a
custom resource's kind, so the last one runs in JSON mode (`test_utils.policy(json_mode=True)`):
its CEL `validations` are evaluated, its admission match is not.
