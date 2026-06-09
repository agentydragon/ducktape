# KubeVirt NixOS VM Runbook

KubeVirt and CDI are installed from <k8s/kubevirt/>. The paved end-to-end flow
for a new NixOS VM is:

1. Publish the bootstrap qcow2 to SeaweedFS — `cluster/k8s/vm-images-publisher/`.
2. Define the VM under `cluster/k8s/<name>/` with a CDI `DataVolume` sourcing
   that qcow2 — see <k8s/gecko/> as the canonical example.
3. Boot, SSH in with a key in `nix/nixos/hosts/bootstrap/default.nix`, then
   `nixos-rebuild switch --flake github:agentydragon/ducktape?ref=devel#<name>`
   to take on a real host config.

## Publishing The Bootstrap Image

Trigger an in-cluster build + upload from the suspended CronJob:

```bash
kubectl create job --from=cronjob/vm-images-publisher \
  "publish-$(date +%s)" -n vm-images-publisher
```

See <k8s/vm-images-publisher/README.md> for environment overrides (publish a
non-default ref, alternate flake output, etc.). The resulting object key is
`bootstrap/<commit-sha>.qcow2`.

## Wiring A VM

Crib from <k8s/gecko/>:

- `namespace/` — dedicated namespace.
- `app/vm-images-s3-reader.yaml` — `ExternalSecret` pulling `cdiReader*` keys
  from `seaweedfs/vm-images-s3-credentials` via cross-namespace `SecretStore`.
- `app/datavolume.yaml` — points at the published qcow2 via the public
  `s3.allegedly.works` endpoint (reads work over that path; only writes were
  ever slow).
- `app/virtualmachine.yaml` — `VirtualMachine` with UEFI (`secureBoot: false`),
  virtio rootdisk + NIC, `runStrategy: Always`.
- `app/service.yaml` — ClusterIP exposing SSH at :22.

Both `namespace` and `app` are wired as separate Flux Kustomizations with
`wait: true` health checks on the DataVolume + VirtualMachine, so the chain
won't report Ready until CDI finishes the import and the VM is up.

## Verification

```bash
# Guest agent boot check (also exposes IP, hostname, kernel)
kubectl -n <ns> get vmi <name> -o jsonpath='{.status.guestOSInfo}{"\n"}'

# SSH via port-forward (use before public exposure is wired)
kubectl -n <ns> port-forward svc/<name>-ssh 2222:22
ssh -p 2222 agentydragon@127.0.0.1
```

The bootstrap NixOS config (`nix/nixos/hosts/bootstrap/default.nix`) authorises
SSH keys for `wyrm2`, `atlas`, `rugged`. To SSH from a workstation whose key
isn't in that list, add the public key to that file and re-publish the image.

## Exposing SSH Publicly

Cilium 1.19's Gateway API controller does not implement `TCPRoute`, so a
`protocol: TCP` listener on `cluster-gateway` never gets a corresponding
Envoy listener. The workaround is a hand-written `CiliumEnvoyConfig` that
declares the listener directly on the `cilium-envoy` DaemonSet. See
<k8s/gecko/app/ciliumenvoyconfig.yaml>: it binds `0.0.0.0:22` on every hil
node (hostNetwork) with a `tcp_proxy` filter pointing at the gecko-ssh
Service. Wildcard `*.allegedly.works` already resolves to those node IPs,
so `ssh agentydragon@gecko.allegedly.works` works for any key in
`nix/nixos/hosts/bootstrap/default.nix`.

To expose a second VM, copy the CEC, bump the listener port (one port per
backend; SSH has no SNI), and update `cluster:` / `backendServices:` to
point at the new Service. Past ~2–3 VMs the port juggling gets clunky;
<plans/vm_ssh_exposure.md> sketches a TLSRoute + `ProxyCommand` path that
multiplexes any number of SSH backends behind the existing `:443` listener
by SNI.

Security model: SSH key-only auth on the VM (NixOS base config disables
`PasswordAuthentication`). Bruteforce attempts against the public :22 are
expected noise against a key-only sshd.

## Caveats

VMs that use `local-path-ovh` (or any local-path class) are tied to one node
and are not live-migratable; node loss = availability loss. For durable VMs,
use a CSI backend with RWX/block support and `VolumeSnapshotClass` before
relying on KubeVirt migration, snapshots, or node-failure recovery.
