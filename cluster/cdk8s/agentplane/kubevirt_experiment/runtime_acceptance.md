# Runner VM acceptance helper

Use [`runtime_acceptance.py`](runtime_acceptance.py) with one disposable guest through
`SandboxInventory` and a localhost runner gRPC port-forward. Record evidence in
[`agentplane/debug/kubevirt/`](../../../../agentplane/debug/kubevirt/).

The fixture returns deterministic Claude/Codex text through the configured proxy. It does **not**
test production credential substitution, destination authorization, TLS interception, or CONNECT.

## Prepare a disposable namespace

Run from the Nix devshell with fresh namespace, digest-pinned images, and public CA bundle.
`agentplane-workload` is a placeholder credential name; provide no credential values.

```bash
suffix=replace-with-unique-id
ns="agentplane-vm-prototype-runtime-$suffix"
out=/tmp/agentplane-vm-runtime-setup
relay_image='git.allegedly.works/ducktape-ci/agentplane-egress-sidecar@sha256:<relay-digest>'
runner_image='git.allegedly.works/ducktape-ci/agentplane-runner-vm@sha256:<runner-digest>'
node=ovh-ns103711
public_ca=/path/to/public-ca.crt

bb run //cluster/cdk8s/agentplane/kubevirt_experiment:main -- --namespace "$ns" \
  --relay-image "$relay_image" --proxy-host "prototype-gateway.$ns.svc.cluster.local" \
  --outdir "$out" --setup --model-fixture --memory-quota 24Gi --cpu-quota 8
kubectl apply -f "$out/launcher-admission.k8s.yaml"
```

The render creates the gateway, TokenReview permission, policy, ESO reader, and pull-secret
reference. Wait for ESO to create `forgejo-images-creds`; it never prints the registry credential.

## Create and probe the guest

`create` uses the provider path and immediately prints the VM name and UID. Defaults are 4 vCPUs,
10 GiB memory, 20 GiB state, and 40 GiB workspace; adjust node and storage class for the cluster.

```bash
bb run //cluster/cdk8s/agentplane/kubevirt_experiment:runtime_acceptance -- create \
  --namespace "$ns" --name runner-runtime --image "$runner_image" \
  --storage-class local-path-ovh-hdd --image-pull-secret forgejo-images-creds \
  --llm-base-url http://model.invalid --proxy-url http://10.0.2.1:3128 \
  --ca-bundle-file "$public_ca" --kubernetes-host kubernetes.default.svc \
  --kubernetes-credential-name agentplane-workload \
  --node-selector "kubernetes.io/hostname=$node"
```

After readiness, run the printed port-forward in another terminal:

```bash
vm='<created VM name>'
vm_uid='<created VM UID>'
target=localhost:17000
bb run //cluster/cdk8s/agentplane/kubevirt_experiment:runtime_acceptance -- initialize --namespace "$ns" --name "$vm" --target "$target"
```

`initialize` calls `ListSessions`, reports UID/GID and cgroup membership, writes a workspace
checksum, and proves `/state/sessions` and cgroup controls are not writable. It does not read
`memory.max`; capture effective limits through privileged QGA read-only inspection. Save
`WORKSPACE_SHA256` as `workspace_sha`.

## Native turns and stop/start recovery

Run both native harnesses; save each JSON result's `session_id`, `source_id`, `after_cursor`, and
`recovery_marker` for recovery.

```bash
bb run //cluster/cdk8s/agentplane/kubevirt_experiment:runtime_acceptance -- session --target "$target" --harness claude --model claude-3-7-sonnet-20250219
bb run //cluster/cdk8s/agentplane/kubevirt_experiment:runtime_acceptance -- session --target "$target" --harness codex --model gpt-5-codex
```

Each turn should return `MARKER_SAVED`. Stop fully and restart through the provider; the helper
waits for the old VMI to disappear and prints the new port-forward command:

```bash
bb run //cluster/cdk8s/agentplane/kubevirt_experiment:runtime_acceptance -- stop-start \
  --namespace "$ns" --name "$vm"
```

Forward the new Pod and run `recover` for each saved session, using its JSON fields. Example:

```bash
bb run //cluster/cdk8s/agentplane/kubevirt_experiment:runtime_acceptance -- recover \
  --target "$target" --session-id '<session_id>' --source-id '<source_id>' \
  --after-cursor '<after_cursor>' --recovery-marker '<recovery_marker>'
```

Recovery checks the same source ID, contiguous cursors, `HarnessStarted.resumed`, and recalled
marker. Verify the initialized workspace file after restart with a fresh setup session:

```bash
cat >workspace-hash.sh <<'EOF'
sha256sum /workspace/agentplane-vm-acceptance/probe.txt | awk '{print "WORKSPACE_SHA256=" $1}'
EOF
bb run //cluster/cdk8s/agentplane/kubevirt_experiment:runtime_acceptance -- setup-probe \
  --target "$target" --harness claude --model claude-3-7-sonnet-20250219 \
  --setup-script-file workspace-hash.sh --expect-marker "WORKSPACE_SHA256=$workspace_sha"
```

## Bounded setup probes

Use a new session ID for each probe. The helper records only validated markers for memory, CPU,
pids, native-history/workspace quota and usage, `QUOTA_RESULT`, and `WORKSPACE_SHA256`. It reports
the session ID before opening the stream and output byte counts, never raw script output. It calls
`ListSessions` and verifies the journal's terminal setup state after every setup; errors include
the exit code and validated markers.

For an expected memory-pressure failure, a script may exceed the per-session memory limit and be
killed by the cgroup. The runner must remain responsive; use `--expect-failure` rather than pinning
an initial signal or exit code:

```bash
bb run //cluster/cdk8s/agentplane/kubevirt_experiment:runtime_acceptance -- setup-probe \
  --target "$target" --harness claude --model claude-3-7-sonnet-20250219 \
  --setup-script-file /path/to/bounded-memory-pressure.sh --expect-failure
```

For the XFS project-quota probe, verify the 7 GiB allocation succeeds and the 9 GiB allocation
fails with `ENOSPC`; print `QUOTA_RESULT=ENOSPC` and return zero, then pass
`--expect-marker QUOTA_RESULT=ENOSPC`. `EDQUOT` is also an allowed filesystem result. Use fresh
sessions to test workspace and native-history quotas separately; requested sizes alone prove no
quota behavior.

## Replace the image while halted

This fixture keeps desired-mode `RUNNING`: halt this VM, wait for VMI deletion, call
`replace-image` through the selected provider template, then `provision` to await readiness:

```bash
kubectl -n "$ns" patch vm "$vm" --type=merge -p '{"spec":{"runStrategy":"Halted"}}'
kubectl -n "$ns" wait --for=delete "vmi/$vm" --timeout=10m
bb run //cluster/cdk8s/agentplane/kubevirt_experiment:runtime_acceptance -- replace-image --namespace "$ns" \
  --name "$vm" --uid "$vm_uid" --template prototype \
  --image 'git.allegedly.works/ducktape-ci/agentplane-runner-vm@sha256:<replacement-digest>'
bb run //cluster/cdk8s/agentplane/kubevirt_experiment:runtime_acceptance -- provision --namespace "$ns" --name "$vm" --uid "$vm_uid"
```

`replace-image` reads the stored template, changes only the digest, and relies on the provider to
check UID, selected template, state schema, halted run strategy, and VMI absence. This manual
halt is specific to this acceptance fixture. Normal lifecycle operations use provider suspend and
resume; `stop-start` exercises that path.

## Cleanup

Save evidence first. Halt the VM and wait for VMI deletion. VM deletion retains its DataVolumes/PVCs;
remove both explicitly before deleting the namespace and cluster-scoped resources:

```bash
kubectl -n "$ns" patch vm "$vm" --type=merge -p '{"spec":{"runStrategy":"Halted"}}'
kubectl -n "$ns" wait --for=delete "vmi/$vm" --timeout=10m
kubectl -n "$ns" delete vm "$vm" --wait=true
kubectl -n "$ns" delete datavolume "vm-$vm-state" "vm-$vm-workspace" --ignore-not-found --wait=true
kubectl -n "$ns" delete pvc "vm-$vm-state" "vm-$vm-workspace" --ignore-not-found --wait=true
kubectl delete namespace "$ns" --wait=true
kubectl delete clusterpolicy "$ns-launcher-relay" --ignore-not-found
kubectl delete clustersecretstore "kubernetes-$ns-secret-store" --ignore-not-found
kubectl delete clusterrole,clusterrolebinding "$ns-reviewer" --ignore-not-found
kubectl -n forgejo-images delete role,rolebinding "$ns-reader" --ignore-not-found
```
