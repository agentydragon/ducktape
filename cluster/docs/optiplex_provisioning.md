# Provision the home OptiPlex Talos worker

Provision the Dell OptiPlex 7060 Micro as the `optiplex` worker in the existing
Talos cluster. This is home bare metal, not a Proxmox VM: it is installed from
USB on the `192.168.1.0/24` LAN and joins the cluster over Nebula.

Tofu owns the machine-config definition and its embedded cluster and Nebula
credentials. Applying that generated config to the machine's maintenance API
is the only imperative installation step.

## Declared identity

| Property                                | Value                                                              |
| --------------------------------------- | ------------------------------------------------------------------ |
| Kubernetes/Talos hostname               | `optiplex`                                                         |
| Talos role                              | worker                                                             |
| Nebula IP                               | `10.42.0.18/16`                                                    |
| Topology region                         | `home`                                                             |
| Topology zone                           | `home-lan`                                                         |
| Storage tier                            | `ssd`                                                              |
| Maintenance address during installation | `192.168.1.89/24` (DHCP; ephemeral)                                |
| NIC                                     | Intel I219-LM `eno1`, MAC `e4:54:e8:85:9f:b2`                      |
| Install disk                            | SK hynix BC511 256 GB, serial `AS9CN54631CA0CT13`                  |
| Stable install path                     | `/dev/disk/by-id/nvme-BC511_NVMe_SK_hynix_256GB_AS9CN54631CA0CT13` |

The machine has no Proxmox provider ID and no dependency on `atlas`.

## 1. Prepare the operator environment

Run from the repository root so direnv supplies the pinned CLIs, Talos
credentials, and SOPS age key:

```bash
direnv allow
eval "$(direnv export bash)"
```

The Tofu PostgreSQL backend is in-cluster. From a non-cluster workstation,
start the port-forward documented by `.envrc` in a separate terminal. If the
generated kubeconfig is stale, use the declared public API endpoint explicitly:

```bash
kubectl --server=https://api.allegedly.works:6443 \
  port-forward -n tofu-state svc/tofu-state-db-ovh-rw 15432:5432
```

## 2. Generate and boot the installation ISO

The existing metal Image Factory schematic includes the Nebula system
extension. Ask Tofu to evaluate its ISO URL directly; this works before the new
output has been written to state:

```bash
cd cluster/terraform/main
tofu init -upgrade
tofu console <<< 'data.talos_image_factory_urls.kimsufi.urls.iso'
```

The ISO and `machine.install.image` use the same schematic. Write the ISO to a
USB drive, boot it via the Dell `F12` UEFI boot menu, and leave it running in
maintenance mode. The non-Secure-Boot ISO requires Secure Boot to be disabled.
Set BIOS power recovery to power on after AC loss.

## 3. Confirm hardware from maintenance mode

Do not copy disk names from another machine. With the current DHCP lease:

```bash
maintenance_ip=192.168.1.89

talosctl get disks --insecure --endpoints="$maintenance_ip" --nodes="$maintenance_ip"
talosctl get links --insecure --endpoints="$maintenance_ip" --nodes="$maintenance_ip"
talosctl get addresses --insecure --endpoints="$maintenance_ip" --nodes="$maintenance_ip"
talosctl get routes --insecure --endpoints="$maintenance_ip" --nodes="$maintenance_ip"
```

Confirm that the NVMe serial and NIC MAC match the declared-identity table.
The USB drive is `/dev/sda`; it is never an install target.

## 4. Materialize the Tofu-owned configuration

Review and apply only the new certificate and generated-config dependency
chain; do not run a full cluster bootstrap for this additive worker:

```bash
# Continue in cluster/terraform/main from step 2.

tofu plan \
  -target='data.talos_machine_configuration.home_worker["optiplex"]'

tofu apply \
  -target='data.talos_machine_configuration.home_worker["optiplex"]'
```

The dependency graph signs `optiplex.nebula.allegedly.works` from the existing
Nebula CA and embeds that certificate, its private key, the Nebula client
configuration, and the existing Talos cluster machine secrets in the generated
worker configuration.

Write the sensitive output to a mode-0600 temporary file and validate it:

```bash
worker_config=$(mktemp --tmpdir optiplex-worker.XXXXXX.yaml)
trap 'rm -f "$worker_config"' EXIT
chmod 0600 "$worker_config"

tofu output -raw optiplex_worker_machine_configuration >"$worker_config"
talosctl validate --config "$worker_config" --mode metal --strict
```

Never commit or retain this file; it contains cluster credentials and the
node's Nebula private key.

## 5. Install and join

This is the destructive step: applying the configuration installs Talos to the
declared NVMe and reboots the machine.

```bash
talosctl apply-config \
  --insecure \
  --endpoints="$maintenance_ip" \
  --nodes="$maintenance_ip" \
  --file "$worker_config"
```

Do **not** run `talosctl bootstrap`; the cluster is already bootstrapped. Remove
the USB when the machine begins rebooting so firmware boots the installed NVMe.

## 6. Verify

After Nebula comes up, address the node directly by its mesh IP while retaining
the existing control-plane endpoints in talosconfig:

```bash
talosctl version --endpoints=147.135.39.162 --nodes=10.42.0.18 --short
talosctl get extensions --endpoints=147.135.39.162 --nodes=10.42.0.18
talosctl service ext-nebula --endpoints=147.135.39.162 --nodes=10.42.0.18

kubectl --server=https://api.allegedly.works:6443 get node optiplex -o wide
kubectl --server=https://api.allegedly.works:6443 get node optiplex \
  -o jsonpath='{.metadata.labels.topology\.kubernetes\.io/region}{" "}{.metadata.labels.topology\.kubernetes\.io/zone}{"\n"}'
```

Expected results:

- node `optiplex` reaches `Ready`;
- Kubernetes `InternalIP` is `10.42.0.18`;
- topology is `home home-lan`;
- the Nebula extension service is running;
- Cilium and the cluster DaemonSets become healthy on the node.

Talos v1.12.3's Linux 6.18 kernel can emit a non-fatal
`REG INVARIANTS VIOLATION` warning while Cilium loads its BPF programs. This is
the upstream kernel verifier regression tracked in
[cilium/cilium#44216](https://github.com/cilium/cilium/issues/44216). Confirm
that it occurred only once, the node remains `Ready`, the Cilium pod has not
restarted, and `cilium-dbg status --brief` reports `OK`. Upgrade Talos when the
selected release's kernel contains the upstream verifier fix; treat repeated
warnings or rejected BPF programs as a network failure, not harmless console
noise.

Finally reboot once through the Talos API and confirm it returns without the
USB drive. Test AC power recovery separately when disruption is acceptable.
