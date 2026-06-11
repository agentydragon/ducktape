# Talos Kubernetes Cluster

Small Talos k8s cluster with GitOps and HTTPS.

- Deploy: Single command `./bootstrap.sh` (automated layered deployment)
- VMs:
  - Run Talos, configured and bootstrapped with Terraform.
  - Disks are pre-baked per-node from Image Factory with static IPs and Tailscale + QEMU guest agent
- VPS forwards traffic to cluster through Tailscale mesh.
- Test application: <https://test.test-cluster.agentydragon.com/>
- CNI: Cilium with Talos-specific security configuration
- Sealed-secrets: Automatic keypair persistence via terraform state for turnkey GitOps

## Prerequisites

- **Proxmox credentials**: Create Proxmox terraform + CSI users (tokens managed in terraform state)
- **SSH access**: `root@atlas` (Proxmox) and `root@agentydragon.com` (Headscale server) for credential generation
- See [docs/BOOTSTRAP.md](docs/BOOTSTRAP.md#credential-setup) for detailed setup instructions

## direnv

`.envrc` auto-exports `KUBECONFIG` and `TALOSCONFIG` and provides CLI tools (kubeseal, talosctl, etc.).
Execute tools like these with the direnv loaded, or use `direnv exec .`.

## Infrastructure

- Network: 10.2.0.0/16, gateway 10.2.0.1
- 5 Talos nodes:
  - 3 controllers (controlplane0-2 = 10.2.1.1-3)
  - 2 workers (worker0-1 = 10.2.2.1-2)
- High availability VIP pools:
  - 10.2.3.1: Cluster Kube API endpoint - kube-vip LB across controller Kube API servers
  - 10.2.3.2 (`ingress-pool`): MetalLB across worker node replicas of NGINX Ingress
  - 10.2.3.3 (`dns-pool`): PowerDNS
  - 10.2.3.4-20 (`services-pool`): for future use (Harbor, Gitea, etc.)
- Domain: `*.test-cluster.agentydragon.com`
  - PowerDNS in k8s has authority on this domain and handles Let's Encrypt DNS-01 challenges
  - cert-manager provisions Let's Encrypt certs
- HTTPS chain: Internet → VPS nginx reads CNI → Tailscale VPN → HA VIP → NGINX Ingress terminates TLS → app

## Services

Deployed services accessible via `*.test-cluster.agentydragon.com`:

- **Authentik (SSO)**: <https://auth.test-cluster.agentydragon.com>
- **Gitea (Git)**: <https://git.test-cluster.agentydragon.com>
- **Harbor (Registry)**: <https://registry.test-cluster.agentydragon.com>
- **Vault (Secrets)**: <https://vault.test-cluster.agentydragon.com>
- **Matrix (Chat)**: <https://chat.test-cluster.agentydragon.com>
- **Grafana (Monitoring)**: <https://grafana.test-cluster.agentydragon.com> (if exposed)
- **Nix Cache**: <https://cache.test-cluster.agentydragon.com> (Harmonia binary cache)
- **Test App**: <https://test.test-cluster.agentydragon.com>

All traffic routes: Internet (443) → VPS nginx (SNI passthrough) → Tailscale →
MetalLB VIP (10.2.3.2:443) → NGINX Ingress → Services

### User Management

Users are declaratively provisioned via tofu-controller with ESO-generated passwords.

**Retrieve user password:**

```bash
kubectl get secret agentydragon-user-password -n flux-system -o jsonpath='{.data.user_password}' | base64 -d
```

**User Details:**

- Username: `agentydragon`
- Email: <agentydragon@gmail.com>
- Group: authentik Admins (admin permissions)
- Password: ESO-generated (32 chars, see command above)

## Secret Management Strategy

**Stable SealedSecret Keypair**: Keypair is generated and stored in terraform state (`terraform/00-persistent-auth/`)
to ensure SealedSecrets always decrypt correctly across cluster recreations.

**Setup**: Run `terraform apply` in `terraform/00-persistent-auth/` once per environment. The keypair
persists in terraform state and survives cluster destroy/recreate cycles.

**Sealing new secrets**:

```bash
# Get public cert from terraform state
cd terraform/00-persistent-auth
terraform output -raw sealed_secrets_public_key > /tmp/sealed-secrets.crt
kubeseal --cert /tmp/sealed-secrets.crt < secret.yaml > sealed-secret.yaml
```

**Bootstrap fail-fast**: Script requires persistent auth layer to exist, prevents keypair mismatches that break GitOps.

## CNI Architecture Decision

**Infrastructure vs GitOps Separation**: Based on circular dependency analysis and industry best practices
(AWS EKS Blueprints, etc.), CNI is managed at the infrastructure layer, not via GitOps.

**Architecture Layers:**

- **Talos**: CoreDNS
- **Terraform**: CNI (Cilium)
- **Flux**: Applications only

**Why CNI Cannot Be GitOps-Managed:**

- Circular dependency: GitOps tools need networking to function, but would be managing their own networking
- Network disruption during handoffs: When Flux tries to update Terraform-installed CNI, worker nodes become
  permanently NotReady due to container image pull failures during networking gaps
- Industry pattern: Major platforms (AWS EKS, GKE Autopilot) manage CNI at infrastructure layer

## Repository Structure

```text
cluster/
├── shell.nix, .envrc      # direnv (KUBECONFIG, TALOSCONFIG, kubeseal CLI, ...)
├── docs/
│   ├── BOOTSTRAP.md       # Bootstrap procedure from empty Proxmox
│   ├── OPERATIONS.md      # Management, troubleshooting commands
│   └── PLAN.md            # Future roadmap, strategic decisions
├── CLAUDE.md, AGENTS.md   # Instructions for AI agents
├── terraform/
│   ├── infrastructure/    # Provisioning from empty Proxmox; boots Talos, Kube, Cilium; hands off to Flux
│   │   ├── cilium/        # CNI configuration (Terraform-managed, not GitOps)
│   │   ├── talosconfig    # Creds for node Talos APIs (generated, gitignored)
│   │   ├── kubeconfig     # Kube config (generated, gitignored)
│   │   ├── modules/talos-node/ # Reusable Talos node module
│   │   └── tmp/           # Temporary files (e.g., per-node baked Talos disk images)
│   └── gitops/            # tofu-controller managed Terraform
│       ├── authentik/     # Authentik SSO provider configuration
│       ├── vault/         # Vault configuration
│       ├── secrets/       # Secret generation
│       ├── services/      # Service integration configs
│       └── users/         # User provisioning via Terraform
├── k8s/                   # Kubernetes manifests (Flux-managed applications only)
│   ├── core/              # CRDs and controllers (sealed-secrets, tofu-controller)
│   ├── metallb/           # Load balancer
│   ├── cert-manager/
│   ├── ingress-nginx/     # HTTP(S) ingress
│   ├── powerdns/          # DNS server (external)
│   ├── vault/, external-secrets/  # Secret synchronization
│   ├── authentik/         # Identity and SSO provider
│   ├── sso/               # SSO integrations and user management
│   │   └── users/         # User provisioning manifests
│   ├── services-config/   # Authentik SSO config for services, via Terraform
│   └── applications/
│       ├── harbor/        # Container registry
│       └── gitea/, matrix/, test-app/
└── flux-system/           # Flux controllers (auto-generated)
```

## How Things Are Wired Together

### Network Architecture

Internet (443) → VPS nginx proxy → Tailscale VPN → MetalLB VIP (10.2.3.2:443) → NGINX Ingress → Apps

- VPS: `~/code/ducktape/ansible/nginx-sites/test-cluster.agentydragon.com.j2`
- DNS:
  - Cluster PowerDNS (10.2.3.3) is primary authoritative server
  - VPS PowerDNS is secondary, replicates zone via AXFR over Tailscale
  - TCP MTU probing enabled for PMTUD blackhole mitigation (see `docs/AXFR_DEBUGGING.md`)
  - Cluster PowerDNS handles Let's Encrypt DNS-01 challenges to obtain SSL certs
- LoadBalancer: NGINX Ingress uses MetalLB VIP 10.2.3.2 instead of NodePort
- Cilium: `kubeProxyReplacement: true` with privileged port protection enabled

- Terraform → Image Factory API → Custom QCOW2 with META key 10 → VMs with static IPs (no DHCP)
- GitOps flow: Git commit → Flux detects change → applies k8s manifests
- Deployment path: `k8s/` directory → Flux Kustomizations → HelmReleases → Running pods
- Secret management: local `kubeseal` → sealed-secrets controller → K8s Secret → Application pods

Kube VIP (10.2.3.1) is established after cluster formation, so bootstrap instead runs against first controller (10.2.1.1).

## Let's Encrypt Rate Limits

**IMPORTANT**: Let's Encrypt has strict rate limits that affect repeated testing:

**Duplicate Certificate Limit**: 5 certificates per week for the same exact domain name

- Applies per domain (e.g., `registry.test-cluster.agentydragon.com`)
- Rolling 7-day window, refills at ~1 cert per 34 hours
- No overrides available
- **Problem**: Each `terraform destroy && ./bootstrap.sh` cycle requests fresh certificates

**For Development/Testing**: Use Let's Encrypt **staging environment**

- Staging limit: 30,000 certificates per week (vs production's 5)
- Certificates are untrusted (browser warnings) but functional
- Switch to production once deployment is stable

**If rate limited**: cert-manager will auto-retry on exponential backoff after the limit expires.
To force immediate retry after reset:

```bash
kubectl delete certificaterequest -A -l cert-manager.io/certificate-name
```

See: <https://letsencrypt.org/docs/rate-limits/>

## Prerequisites / external dependencies

- direnv configured in cluster directory
- VM hosting: Proxmox host `atlas` with SSH access
- GitHub for Flux
- VPS: nginx proxy and PowerDNS for external connectivity, configured in `~/code/ducktape` repo:
  - nginx: `ansible/nginx-sites/`
  - PowerDNS: `ansible/host_vars/vps/powerdns.yml`
