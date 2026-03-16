# lxc-k8s-test — NixOS LXC Container on Proxmox

Test container for running a k8s worker node in LXC on atlas.
Joins the Talos k8s cluster via KubeSpan mesh (like wyrm2/rugged).

## Why Not Terraform?

Proxmox requires `root@pam` (not API tokens) to set feature flags (`nesting`, `keyctl`)
on privileged containers. API tokens (even `root@pam!tofu`) fail with:

> Permission check failed (changing feature flags for privileged container is only
> allowed for root@pam)

This is a hardcoded Proxmox restriction — no privilege grant can bypass it. The container
is managed via `pct` on atlas instead.

## Deploy

### 1. Build and upload the NixOS LXC tarball

```bash
cd ~/code/ducktape
nix build .#lxc-k8s-test-lxc -o /tmp/lxc-k8s-test-lxc
ssh root@atlas "mkdir -p /var/lib/vz/template/cache"
scp /tmp/lxc-k8s-test-lxc/tarball/*.tar.xz \
  root@atlas:/var/lib/vz/template/cache/lxc-k8s-test.tar.xz
```

### 2. Create the container

```bash
ssh root@atlas "pct create 200 local:vztmpl/lxc-k8s-test.tar.xz \
  --hostname lxc-k8s-test \
  --ostype nixos \
  --unprivileged 0 \
  --cores 4 \
  --memory 8192 \
  --swap 0 \
  --rootfs local-zfs:50 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --features nesting=1,keyctl=1 \
  --start 1 \
  --onboot 1"
```

**Important**: `ip=dhcp` in `--net0` is required. Without it, `proxmox-lxc.nix` writes
`DHCP = no` to the systemd-networkd config and eth0 gets no IPv4 address.

### 3. Verify

```bash
ssh root@atlas "lxc-info -n 200"           # Check IP
ssh root@<container-ip> "hostname && uname -a"  # Verify SSH
```

## Update

To deploy NixOS config changes, rebuild the tarball and recreate the container:

```bash
# Rebuild
nix build .#lxc-k8s-test-lxc -o /tmp/lxc-k8s-test-lxc
scp /tmp/lxc-k8s-test-lxc/tarball/*.tar.xz \
  root@atlas:/var/lib/vz/template/cache/lxc-k8s-test.tar.xz

# Recreate (destroys data!)
ssh root@atlas "pct stop 200 && pct destroy 200"
# Then re-run the pct create command above
```

## Destroy

```bash
ssh root@atlas "pct stop 200 && pct destroy 200"
```

## K8s Cluster Join

K8s credentials must be placed manually after boot (no cloud-init in LXC):

- `/etc/kubespan/agent.yaml`
- `/etc/kubernetes/pki/ca.crt`
- `/etc/kubernetes/bootstrap-kubelet.conf`

## Specs

| Setting    | Value           |
| ---------- | --------------- |
| CT ID      | 200             |
| vCPUs      | 4               |
| Memory     | 8 GB            |
| Disk       | 50 GB           |
| Storage    | local-zfs       |
| Network    | vmbr0           |
| Privileged | yes             |
| Features   | nesting, keyctl |

## TODO: NVIDIA GPU Passthrough

See <TODO.md> for GPU passthrough plans.
