# Disk Encryption & Node Security Notes

Notes from investigation on 2026-04-11. Context: cluster runs Talos Linux on
OVH/Kimsufi bare metal plus NixOS/Proxmox workers. Currently **no disk
encryption is enabled** on any node.

## Threat Model

| Threat                          | Example                                 | What's exposed                               |
| ------------------------------- | --------------------------------------- | -------------------------------------------- |
| Disk removed from machine       | Drive RMA, decommission                 | etcd data, secrets, certs                    |
| Whole machine stolen (offline)  | Datacenter break-in, shipping intercept | Everything on disk                           |
| Whole machine stolen (online)   | Attacker boots it on their network      | Kubelet creds, mounted secrets               |
| Hosting provider is adversarial | Provider images disk, accesses console  | Everything — RAM, disk, network metadata     |
| Root on a running node          | Kernel exploit, container escape        | Secrets for pods on that node, kubelet creds |

## What Kubernetes Stores and Where

- **etcd** (control plane nodes): all Secrets, ConfigMaps, ServiceAccounts, tokens —
  plaintext by default. Talos enables `aescbc` encryption at rest, but the key is in the
  machine config on the same disk.
- **Kubelet** (all nodes): client cert/key for API server, projected service account tokens
  for scheduled pods, pulled secret volumes.
- **Container filesystems**: application secrets mounted as volumes or env vars.

## Talos Disk Encryption Options

Configured via `machine.systemDiskEncryption` in machine config. Uses LUKS2. Multiple
key slots can be combined.

| Provider | Key source                   | Protects against                              |
| -------- | ---------------------------- | --------------------------------------------- |
| `static` | Passphrase in machine config | Nothing useful (key on same disk)             |
| `nodeID` | Derived from node UUID       | Disk moved to different machine               |
| `tpm`    | Sealed to TPM PCR registers  | Disk removed from machine                     |
| `kms`    | Sealed by remote KMS (Omni)  | Whole machine theft (needs network to unseal) |

### TPM Details

- Seals to PCR values (default PCR 7 = SecureBoot state)
- Best with SecureBoot — ensures only verified Talos can unlock
- **Does not help against whole-machine theft** — attacker boots the machine normally
- **No TPM+PIN support** in Talos
- Proxmox VMs can use vTPM; dedicated servers may have physical TPM, but verify
  per model and provider boot chain

### KMS (Omni)

- Node generates random AES256 key → Omni seals it → stored in LUKS2 metadata
- On reboot, node contacts Omni over SideroLink (WireGuard) to unseal
- Stolen machine on different network → can't reach Omni → can't decrypt
- When node is wiped in Omni, key is deleted

## Omni

Sidero Labs' management plane for Talos. Provides unified UI/API for hardware, OS,
and Kubernetes management.

### Deployment Options

| Option                           | Cost                               | Availability burden |
| -------------------------------- | ---------------------------------- | ------------------- |
| **SaaS** (Hobby)                 | $10/mo, 1-10 nodes, non-commercial | Sidero Labs manages |
| **SaaS** (Startup)               | $25/node/mo                        | Sidero Labs manages |
| **Self-hosted** (non-production) | Free (BSL license)                 | You manage          |
| **Self-hosted** (production)     | $100/node/mo Enterprise license    | You manage          |

### Self-Hosted Requirements

- 4 vCPU, 8-16 GB RAM, 500 GB fast SSD (up to ~200 nodes)
- Runs as Helm chart on a Kubernetes cluster
- Needs PostgreSQL + etcd
- Nodes connect outbound via SideroLink (WireGuard) — no inbound ports needed

### Chicken-and-Egg Problem

Omni **cannot run inside the cluster it encrypts** — circular dependency on boot.
Must run elsewhere:

- SaaS — simplest
- Small separate cluster (k3s on a Pi, cheap VPS, different provider)
- A dedicated management VM outside the encrypted cluster

## Hosted Provider Concerns

If you're paranoid about a hosted bare-metal provider:

### What The Provider Can Do

- **Physical access** to the machine
- **IPMI/BMC access** — can mount ISOs, access console, potentially read RAM
- **Disk imaging** during maintenance windows
- **Network metadata** — not content if encrypted, but flow data
- Provider-controlled firmware, remote hands, and boot-chain access remain in
  the trust boundary

### Mitigations

**Against disk reads (decommission, snapshot, RMA):**

- Disk encryption with any provider helps — even `static` passphrase means a raw
  disk image is encrypted (though key placement determines how useful that is)

**Against a determined adversarial provider:**

- **Don't store high-value secrets on their infrastructure.** Use external secret
  stores or inject secrets at runtime.
- **Minimize blast radius**: etcd on hosted nodes puts the provider in the control
  plane trust boundary. If that risk becomes unacceptable, keep control-plane
  nodes and highest-value secrets on trusted hardware.
- Workers only get secrets for pods scheduled on them (`NodeRestriction` admission
  controller, enabled by default).
- **Short-lived credentials**: workload identity, projected service account tokens,
  rotated frequently.
- **Encrypt application data at rest** with keys stored outside the node/provider.

**Network:**

- Nebula mesh already encrypts node-to-node traffic (you have this)
- API server traffic is TLS
- But the hosting provider sees WireGuard/Nebula handshake metadata (which nodes
  talk, when, volume)

### Realistic Assessment

For a homelab/personal infra, the realistic threats from a hosting provider are:

1. **Negligible**: Provider actively attacking you (they'd lose their business)
2. **Low but real**: Provider employee going rogue, or law enforcement compelling access
3. **Moderate**: Decommissioned hardware not being properly wiped

Practical posture:

- **Enable disk encryption** (`tpm` on Proxmox where vTPM exists, `kms` via Omni for
  full protection)
- **Use trusted hardware for the most sensitive workloads** if the hosted-provider
  trust boundary is unacceptable
- **External secrets** for anything truly sensitive
- **Accept the provider trust boundary** for hosted nodes

## Action Items

- [ ] Enable `machine.systemDiskEncryption` on Proxmox nodes (vTPM available)
- [ ] Evaluate Omni SaaS Hobby tier ($10/mo) for KMS encryption across all nodes
- [ ] Audit what secrets are accessible on OVH nodes specifically
- [ ] Decide whether OVH-hosted control-plane nodes are acceptable for this threat model
- [ ] Review `NodeRestriction` admission controller is enabled
