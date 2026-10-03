# Runner VM acceptance

Use [`runtime_acceptance.py`](runtime_acceptance.py) for the disposable guest lifecycle. Its
KubeVirt operations are fixture-local; it does not call the production Sandbox Service. Read
VM, VMI, and Pod identity plus raw `.status` with `inspect`.

## Inputs and setup

Use a unique `agentplane-vm-prototype-*` namespace, digest-pinned relay and runner images, a
storage class and node that can run the VM, and an ESO-created `forgejo-images-creds` Secret.
The trust ConfigMap must contain `ca-certificates.crt` and passwordless `ca-certificates.p12`.
The guest kubeconfig has no credential; this fixture does not test guest Kubernetes access.

Set these values from the image build and the cluster's existing trust-manager Bundle:

```bash
relay_image='git.allegedly.works/ducktape-ci/agentplane-egress-sidecar@sha256:<digest>'
runner_image='git.allegedly.works/ducktape-ci/agentplane-runner-vm@sha256:<digest>'
replacement_image='git.allegedly.works/ducktape-ci/agentplane-runner-vm@sha256:<replacement-digest>'
trust_config_map=agentplane-vm-trust
node=ovh-ns103711
```

Render the admission policy and deterministic model fixture, then apply the generated file:

```bash
ns=agentplane-vm-prototype-runtime-$(date +%s)
out=/tmp/agentplane-vm-runtime-setup
bb run //cluster/cdk8s/agentplane/kubevirt_experiment:main -- --namespace "$ns" \
  --relay-image "$relay_image" --proxy-host "prototype-gateway.$ns.svc.cluster.local" \
  --outdir "$out" --setup --model-fixture
kubectl apply -f "$out/launcher-admission.k8s.yaml"
```

Copy the existing public trust-manager ConfigMap output into the namespace. Preserve `binaryData`
for the PKCS#12 file; do not copy its Secret or private key.

```bash
kubectl get configmap agentplane-testing-egress-ca -n agentplane-testing -o json \
  | jq --arg name "$trust_config_map" --arg namespace "$ns" \
    '{apiVersion, kind, metadata: {name: $name, namespace: $namespace}, data, binaryData}' \
  | kubectl apply -f -
```

## Create and exercise

Create emits JSON lines for VM creation and readiness. Save the created VM identity; its status is
raw KubeVirt status. The VM starts only after blank-disk ownership/input checks so
WaitForFirstConsumer storage can bind through the VMI.

```bash
create_output=$(bb run //cluster/cdk8s/agentplane/kubevirt_experiment:runtime_acceptance -- create \
  --namespace "$ns" --name runner --image "$runner_image" \
  --storage-class local-path-ovh-hdd --llm-base-url http://model.invalid \
  --proxy-url http://10.0.2.1:3128 --ca-bundle-config-map "$trust_config_map" \
  --kubernetes-host kubernetes.default.svc --node-selector "kubernetes.io/hostname=$node")
printf '%s\n' "$create_output"
vm=$(jq -rs 'map(select(.action == "created"))[-1].vm.identity.name' <<<"$create_output")
vm_uid=$(jq -rs 'map(select(.action == "created"))[-1].vm.identity.uid' <<<"$create_output")
```

Run the printed port-forward in another terminal. `initialize` verifies the guest RPC and retires
the one-shot disk-format permission after success.

```bash
target=localhost:17000
bb run //cluster/cdk8s/agentplane/kubevirt_experiment:runtime_acceptance -- inspect \
  --namespace "$ns" --name "$vm"
bb run //cluster/cdk8s/agentplane/kubevirt_experiment:runtime_acceptance -- initialize \
  --namespace "$ns" --name "$vm" --uid "$vm_uid" --target "$target"
session_output=$(bb run //cluster/cdk8s/agentplane/kubevirt_experiment:runtime_acceptance -- session \
  --target "$target" --harness claude --model claude-3-7-sonnet-20250219)
printf '%s\n' "$session_output"
session_id=$(jq -r '.session_id' <<<"$session_output")
source_id=$(jq -r '.source_id' <<<"$session_output")
after_cursor=$(jq -r '.after_cursor' <<<"$session_output")
recovery_marker=$(jq -r '.recovery_marker' <<<"$session_output")
bb run //cluster/cdk8s/agentplane/kubevirt_experiment:runtime_acceptance -- stop-start \
  --namespace "$ns" --name "$vm"
```

After `stop-start`, forward the new launcher Pod, then verify continuation:

```bash
bb run //cluster/cdk8s/agentplane/kubevirt_experiment:runtime_acceptance -- recover \
  --target "$target" --session-id "$session_id" --source-id "$source_id" \
  --after-cursor "$after_cursor" --recovery-marker "$recovery_marker"
```

`setup-probe` reports validated markers and byte counts, not script output:

```bash
printf 'printf "SETUP_PROBE_OK\\n"\n' >/tmp/agentplane-vm-setup-probe.sh
bb run //cluster/cdk8s/agentplane/kubevirt_experiment:runtime_acceptance -- setup-probe \
  --target "$target" --harness codex --model gpt-5-codex \
  --setup-script-file /tmp/agentplane-vm-setup-probe.sh --expect-output SETUP_PROBE_OK
```

For interruption recovery, use `resume --namespace "$ns" --name "$vm" --uid "$vm_uid"` after
the exact VMI has been deleted.

Root replacement requires the VM UID, `runStrategy: Halted`, and no VMI; it patches only the root
containerDisk image. Halt and wait for VMI deletion before calling it:

```bash
kubectl -n "$ns" patch vm "$vm" --type=merge -p '{"spec":{"runStrategy":"Halted"}}'
kubectl -n "$ns" wait --for=delete "vmi/$vm" --timeout=10m
bb run //cluster/cdk8s/agentplane/kubevirt_experiment:runtime_acceptance -- replace-image \
  --namespace "$ns" --name "$vm" --uid "$vm_uid" --image "$replacement_image"
bb run //cluster/cdk8s/agentplane/kubevirt_experiment:runtime_acceptance -- resume \
  --namespace "$ns" --name "$vm" --uid "$vm_uid"
```

## Cleanup and limits

VM deletion leaves state/workspace DataVolumes and PVCs. Delete them only after saving evidence and
deciding their data is disposable, then delete the namespace and experiment's cluster-scoped
policy, secret store, reviewer role/binding, and Forgejo reader role/binding.

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

The direct driver and trust-store boot path have not yet been rerun live; recorded evidence uses an
earlier driver and image. The model fixture returns deterministic text and uses TokenReview for the
relay identity. It does not validate production credential substitution, destination authorization,
TLS interception, or CONNECT. Recorded runtime findings and measured limits are in
[`agentplane/debug/kubevirt/runtime-20261003.md`](../../../../agentplane/debug/kubevirt/runtime-20261003.md).
