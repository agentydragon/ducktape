# Firecracker environment primitive spike

Standalone manual experiment for Agentplane's VM prerequisites. It does not add a Sandbox backend,
controller, or deployed service. Results and integration gaps live in
[the investigation report](../../debug/2026_11_02_firecracker_spike.md).

The guest uses a purpose-built Linux kernel and Ubuntu root filesystem. Its separate read-only
EROFS disk is the **actual source-built Agentplane runner disk**. A synthetic Python workload owns
observable RAM, a child process, an open workspace file and a SQLite database. The launcher runs the
**actual source-built egress sidecar** against a synthetic upstream that validates test credentials
and tunnels only `example.com:443`. No real upstream credentials enter the experiment.

## Reproduce

Requires an x86-64 Kubernetes node with working KVM, the KubeVirt KVM device resource, permission to
create the privileged launcher Pod, and Internet access to the pinned build inputs. The launcher
uses its own network namespace; do not run the probe on the host or with `hostNetwork`.
`launcher.yaml` selects `wyrm2`; change the selector for another suitable node. The privileged Pod
is an experimental convenience, not a production jailer or tenant isolation design.

Use an unused namespace or verify that the fixed `firecracker-spike` namespace belongs to this
experiment before applying. The following commands use the repository Nix devshell. Keep the
checkout and downloaded artifacts under the task scratch directory required by `AGENTS.md`.

```bash
SPIKE_SCRATCH="${TMPDIR:-/tmp}/agents/2026_11_02/firecracker-spike"
mkdir -p "$SPIKE_SCRATCH"
bbr build //agentplane/x/firecracker_spike:agentplane_inputs \
  --remote_download_regex='.*agentplane_inputs.tar$'
kubectl apply -f agentplane/x/firecracker_spike/launcher.yaml
kubectl wait -n firecracker-spike pod/launcher --for=condition=Ready --timeout=180s
kubectl exec -n firecracker-spike launcher -- mkdir -p /work/src /work/artifacts
kubectl cp agentplane/x/firecracker_spike/. firecracker-spike/launcher:/work/src/
kubectl cp bb-out/bazel-out/k8-fastbuild/bin/agentplane/x/firecracker_spike/agentplane_inputs.tar \
  firecracker-spike/launcher:/work/artifacts/agentplane-inputs.tar
kubectl exec -n firecracker-spike launcher -- bash /work/src/prepare.sh
kubectl exec -n firecracker-spike launcher -- python3 /work/src/demo_spike.py
```

`prepare.sh` verifies pinned Firecracker and Linux source hashes, builds the guest kernel with
EROFS and ISO9660 support, and assembles independent block images. It preserves existing images;
use a fresh disposable launcher to rebuild guest source changes. Package versions come from the
Ubuntu repositories at execution time; input and output hashes record the actual build, rather
than promising bit-for-bit reproducibility.

The probe runs once per launcher. It creates a TAP, pod-local firewall rules, working disks and
snapshot files. `results.json` records assertions; guest console and relay logs accompany it.
Full RAM snapshots contain guest memory and stay outside Git. Download evidence before cleanup:

```bash
kubectl cp firecracker-spike/launcher:/work/evidence "$SPIKE_SCRATCH/evidence"
kubectl cp firecracker-spike/launcher:/work/artifacts/input-hashes.txt "$SPIKE_SCRATCH/input-hashes.txt"
kubectl delete namespace firecracker-spike
```

## Interpretation

RAM restore requires the saved memory/state files **and every backing disk at the same boundary**.
The experiment kills each continuation before starting another and restores a saved disk set
before reusing a snapshot. It proves no concurrent-clone or cross-node ownership safety.

The synthetic upstream does not exercise central egress TokenReview, destination policy, MITM,
provider credential substitution, Cilium policies, or real Gateway mTLS. The synthetic workload does
not exercise the Runtime's systemd supervision, native harness recovery, or control-plane identity
fencing. Those are future integration acceptance requirements, listed in the report.
