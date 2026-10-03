# KubeVirt launcher prototype

This is PR 1 of the VM environment stack: opt-in platform admission and a disposable
experiment. It does not enable VM environments in staging/testing. The guest runtime
and Sandbox Service integration are separate changes.

## What this establishes

`//cluster/cdk8s/agentplane/kubevirt_experiment:policy` injects the existing relay into KubeVirt's
launcher Pod, with rotating projected tokens mounted only by the relay. CREATE
admission checks the actual KubeVirt controller caller, live Pod → VMI → VM UIDs,
managed VM label, approved template, and VM-owned ServiceAccount. UPDATE admission
also rejects token mounts in compute, init, and ephemeral containers. API lookup
errors fail admission. Strategic merge is idempotent under webhook reinvocation.

The constructor takes approved template names, relay image and central proxy host.
Namespaces must be dedicated to Agentplane-managed VMs with respect to their KubeVirt
workloads: every `kubevirt.io=virt-launcher` Pod there is checked. The platform must
restrict direct Pod/VM/VMI writes separately; labels and owner references alone do
not establish authority. The production provider must create the VM halted, provision
its owned account and grants, and only then start it.

This prototype uses KubeVirt's masquerade guest gateway (`10.0.2.1`) for the relay.
Only guest SSH (diagnostics) and port 7000 are forwarded. The launcher NetworkPolicy
allows DNS and the synthetic gateway, and admits port 7000 only from that gateway's
Pod label. A real deployment must replace that caller selector with Sandbox Service.
SSH is accessed by localhost-only Kubernetes port forwarding during the experiment.

The synthetic `token_review_gateway.py` checks Kubernetes TokenReview and returns
only the reviewed account/Pod identity. It does **not** implement the production
egress gateway's credential policies, destination authorization, TLS interception,
or CONNECT protocol. Passing this experiment is not full egress acceptance.

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

An admission-service outage remains a separate isolated-control-plane experiment:
this work does not stop shared Kyverno to test `failurePolicy: Fail`. Production
credential substitution/CONNECT and runner gRPC need the subsequent integration
acceptance. Persistent disk recovery, guest OOM budgets, and image replacement belong
to the next two PRs. Nothing here promises live migration or node-loss recovery.
