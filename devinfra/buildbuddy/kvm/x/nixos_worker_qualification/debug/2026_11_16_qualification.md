# Prebuilt worker image qualification, 2026-11-16

Status: image build passed; live qualification stopped before VM creation at
the managed registry credential prerequisite. Guest boot, executor registration,
nested KVM, real harnesses, drain and latency remain unmeasured.

## Artifact and checks

The image implementation was built from commit
`c01bd1dca584311a25e2cc0a382bb1eceb70b30b`, based on
`ac24fb8538975130b2bd77a6f2f8d483d71a5bd2`.

```text
git.allegedly.works/ducktape-ci/buildbuddy-kvm-worker-spike@sha256:3f9df2f2d130a47ba44f795468bfe67d51a20144e30e1bb44a3574f5b8fb4ac0
```

`nix build .#buildbuddy-kvm-worker-spike-container-disk --max-jobs 2 --cores 4`
succeeded. The compressed archive is 1,458,599,068 bytes, so cold containerDisk
pull time needs measurement separately from guest startup. The build includes
Docker 29.9.2, Linux 6.18.60, and the locally archived executor digest recorded in
the image module. These versions have not yet been exercised in the canary.
The candidate was pushed under a unique spike tag using the existing managed
registry credential; the temporary authentication file was removed afterward.

| Check                                      | Result and evidence                                                                                                           |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------- |
| Image workflow roster/path tests           | [Passed](https://app.buildbuddy.io/invocation/8f77b9f3-7567-4805-98b9-d9f386c3757b)                                           |
| Renderer and three manual target binaries  | [Built](https://app.buildbuddy.io/invocation/7c31e02c-ceda-4450-b894-d009f37cf150); test bodies were not executed             |
| Candidate manifest rendering               | [Passed](https://app.buildbuddy.io/invocation/85c8d972-45e0-483a-93b4-fb144155708d)                                           |
| Kubernetes admission                       | Five-resource server-side dry run passed                                                                                      |
| Scoped formatting and lint                 | Passed                                                                                                                        |
| Registry access source/generated manifests | [Drift test passed](https://app.buildbuddy.io/invocation/038da8d9-34ec-41c6-b9e7-f7f4370f8bf5); pre-commit/kubeconform passed |

## Live prerequisite failure

At `2026-11-17T01:11:38Z`, the temporary canary's ExternalSecret reported
`Ready=False`, `SecretSyncedError`. Its event identified the exact boundary:

```text
using cluster store "kubernetes-forgejo-images-secret-store" is not allowed
from namespace "buildbuddy-executor": denied by spec.condition
```

The owning generator has an explicit namespace allowlist that does not include
`buildbuddy-executor`. This is independent of guest startup. A two-line source
and regenerated-manifest change is prepared separately for review; it grants
that namespace access to the existing managed Forgejo registry credential.

Automatic approval review rejected a live allowlist patch because it would
expand a persistent credential-access boundary beyond the isolated canary.
The patch did not execute. No manual Secret copy, alternate credential path,
or token creation was attempted. The store's spec was verified unchanged.

The temporary ConfigMap, ExternalSecret, network policy and metrics Service
were removed. No canary VM, VMI, launcher or work PVC was created. The deployed
Ubuntu worker pool, autoscaling and required test routing were not changed.

## Remaining experiment

After the registry-access change is reviewed, merged and reconciled by Flux,
follow the qualification README with the immutable candidate above. Verify the
managed pull secret is Ready before creating the pool. Record containerDisk
pull, VMI Running, guest startup and scheduler registration as separate clocks.
Use a unique test environment run ID with `--nocache_test_results` and confirm
fresh executions of the KVM smoke and unchanged Firecracker/VM cases. Then
probe an in-flight drain and verify cleanup. One successful canary alone will
not establish a population latency improvement or qualify production cutover.
