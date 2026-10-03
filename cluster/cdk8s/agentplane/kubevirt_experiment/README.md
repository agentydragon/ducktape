# KubeVirt launcher prototype

This directory contains opt-in platform admission and disposable KubeVirt experiments. The
runner guest CLI uses direct, UID-checked KubeVirt operations; it does not enable VM environments
in staging/testing or add a production Sandbox Service provider, app API, or proto kind.

## Code map and boundary

- [`policy.py`](policy.py): launcher admission and relay token mounts.
- [`setup.py`](setup.py): disposable namespace, quotas, ESO pull secret, and synthetic gateway.
- [`vm.py`](vm.py): Fedora and runner VM fixtures.
- [`token_review_gateway.py`](token_review_gateway.py) and
  [`runtime_model_gateway.py`](runtime_model_gateway.py): synthetic relay destinations.

Use a dedicated namespace for these KubeVirt workloads and separately restrict direct Pod/VM/VMI
writes; labels and owner references alone do not establish authority. Neither gateway implements
production credential substitution, destination authorization, TLS interception, or CONNECT.
Runner inputs, commands, probes, and cleanup are in the
[`runtime acceptance guide`](runtime_acceptance.md); guest build and measured findings are in
[`runtime.md`](runtime.md).

## Reproduce

Use an isolated namespace and two small VMs. Never use an existing environment's VM
or state disks. Load the repository's Nix devshell and use Bazel outside the sandbox.
The renderer creates a narrowly scoped ESO store and reader role in `forgejo-images`;
ESO provisions the pull secret. No token is copied through a script or terminal.

```bash
ns=agentplane-vm-prototype-20261003
relay=git.allegedly.works/ducktape-ci/agentplane-egress-sidecar:devel-20260924234050-4819c98
guest=quay.io/containerdisks/fedora@sha256:33d4d276fc28e2141632bc8aed32b1c62a5870bc13ae2e02c75dd0bec8f79cee
ssh-keygen -t ed25519 -N '' -f /tmp/agentplane-vm-prototype-key
bb run //cluster/cdk8s/agentplane/kubevirt_experiment:main -- \
  --namespace "$ns" --relay-image "$relay" \
  --proxy-host "prototype-gateway.$ns.svc.cluster.local" \
  --outdir /tmp/agentplane-vm-setup --setup
kubectl apply -f /tmp/agentplane-vm-setup/launcher-admission.k8s.yaml

# Repeat for vm-a and vm-b. The first render creates the halted VM.
name=vm-a
bb run //cluster/cdk8s/agentplane/kubevirt_experiment:main -- \
  --namespace "$ns" --relay-image "$relay" \
  --proxy-host "prototype-gateway.$ns.svc.cluster.local" \
  --outdir "/tmp/agentplane-$name" --vm-name "$name" \
  --guest-image "$guest" --public-key /tmp/agentplane-vm-prototype-key.pub
kubectl apply -f "/tmp/agentplane-$name/launcher-admission.k8s.yaml"
vm_uid=$(kubectl -n "$ns" get vm "$name" -o jsonpath='{.metadata.uid}')
# Repeat the render above adding --vm-uid "$vm_uid", then apply again.
# This provisions the account with the exact live VM controller owner UID.
kubectl -n "$ns" patch vm "$name" --type=merge -p '{"spec":{"runStrategy":"Always"}}'
```

Keep setup and VM outputs separate: applying a VM fixture again sets it to Halted.
The renderer deliberately requires the live UID instead of guessing an owner.
Before probing, wait for both VMIs and relay containers to become ready.

```bash
pod=$(kubectl -n "$ns" get pod -l kubevirt.io/domain=vm-a -o jsonpath='{.items[0].metadata.name}')
kubectl -n "$ns" port-forward "pod/$pod" 22022:22
# In a second terminal:
ssh -i /tmp/agentplane-vm-prototype-key \
  -o StrictHostKeyChecking=accept-new \
  -o UserKnownHostsFile=/tmp/agentplane-vm-prototype-known-hosts \
  -p 22022 prototype@127.0.0.1
# Inside guest:
curl -i -x http://10.0.2.1:3128 http://synthetic.invalid/probe
curl -i http://prototype-gateway.agentplane-vm-prototype-20261003.svc.cluster.local:8888/probe
```

Check direct internet/peer relay denial, real token rotation without a relay restart,
caller-forgery rejection using server dry-run, and a fresh Pod UID after stop/start.
Do not print the projected bearer token. Issuance/expiry timestamps and reviewed
account/Pod UID are sufficient evidence. Stop the port-forward when done.

## Cleanup

Only delete this experiment's named resources. The test root disks are ephemeral;
there are no user state PVCs in this fixture.

```bash
kubectl delete namespace "$ns" --wait=false
kubectl delete clusterpolicy "$ns-launcher-relay"
kubectl delete clustersecretstore "kubernetes-$ns-secret-store"
kubectl delete clusterrole,clusterrolebinding "$ns-reviewer"
kubectl -n forgejo-images delete role,rolebinding "$ns-reader"
rm /tmp/agentplane-vm-prototype-key /tmp/agentplane-vm-prototype-key.pub
```

## Checks and limits

Run `bbr test //cluster/cdk8s/agentplane/kubevirt_experiment:test_policy` for real Kyverno CLI
mutation/reinvocation and negative admission checks. The test mocks API responses;
it still executes the UID, account, caller, and mount predicates. Live evidence is
recorded in [the experiment log](../../../../agentplane/debug/kubevirt/evidence.md).

This does not stop shared Kyverno to test `failurePolicy: Fail`, or exercise production
credential substitution/CONNECT. The runner acceptance guide exercises gRPC, native-session
recovery, setup probes, stop/start, and root-image replacement on a disposable guest. Nothing here
promises live migration or node-loss recovery.
