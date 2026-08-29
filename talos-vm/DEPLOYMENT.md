# Talos QEMU Deployment - Complete Working Guide

**Date**: 2025-11-18
**Status**: ✅ **VERIFIED** - Full end-to-end deployment successful
**Environment**: Anthropic Claude Code container with authenticated proxy and network restrictions

## Summary

Successfully deployed a Talos Linux v1.9.2 Kubernetes v1.32.0 cluster on QEMU without KVM, in a sandboxed environment with:
- SSL-intercepting proxy with JWT authentication
- DNS-over-HTTPS requirements (standard UDP DNS unreliable)
- No direct internet access (all traffic through proxy)

**Total deployment time**: ~7 minutes from `terraform init` to Ready cluster

## Prerequisites

### Required Software
- Terraform >= 1.5.0
- QEMU (qemu-system-x86_64)
- kubectl
- cloudflared (for DNS-over-HTTPS)
- Python 3 (for HTTPS proxy forwarder)

### Environment Requirements
- `HTTPS_PROXY` / `HTTP_PROXY` environment variable set
- At least 2GB RAM available for VM
- Internet connectivity through proxy

## Deployment Steps

### 1. Install cloudflared

```bash
# Download and install cloudflared
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /tmp/cloudflared
chmod +x /tmp/cloudflared
/tmp/cloudflared --version
```

### 2. Start Required Proxy Services

The deployment requires two proxy services running:

#### Start DNS-over-HTTPS Proxy (cloudflared)

```bash
nohup /tmp/cloudflared proxy-dns --address 0.0.0.0 --port 53 \
  --upstream https://dns.google/dns-query > /tmp/cloudflared.log 2>&1 &
echo $! > /tmp/cloudflared.pid
```

This provides DNS resolution for:
- QEMU VM (uses 10.0.2.3 which forwards to host DNS on port 53)
- Host system DNS resolution

#### Start HTTPS Proxy Forwarder

```bash
cd manual
nohup python3 https-proxy.py > /tmp/https-proxy.log 2>&1 &
echo $! > /tmp/https-proxy.pid
```

This proxy:
- Listens on port 3128 (accessible to QEMU VM as 10.0.2.2:3128)
- Forwards CONNECT requests to upstream authenticated proxy
- Adds JWT authentication automatically from `$HTTPS_PROXY` environment variable
- Allows VM to pull container images without authentication

#### Verify Proxies are Running

```bash
ps aux | grep -E "(cloudflared|https-proxy)" | grep -v grep
tail -f /tmp/cloudflared.log /tmp/https-proxy.log
```

### 3. Deploy with Terraform

```bash
cd terraform
terraform init
terraform apply -auto-approve
```

**Timeline** (approximate):
- 0-1 min: Download Talos kernel and initramfs (~40s)
- 1-2 min: Start VM and wait for maintenance mode (30s)
- 2-4 min: Apply configuration and wait for installation (120s)
- 4-5 min: Bootstrap Kubernetes cluster
- 5-7 min: Wait for cluster to be ready (90s)
- 7+ min: Cluster fully operational with node Ready

The terraform output will show:
```
Apply complete! Resources: 12 added, 0 changed, 0 destroyed.

Outputs:

cluster_endpoint = "https://127.0.0.1:6443"
kubeconfig_file = "./kubeconfig"
talosconfig_file = "./talosconfig"
usage_instructions = <<EOT
...instructions...
EOT
```

### 4. Verify Cluster is Working

```bash
# Set kubeconfig
export KUBECONFIG=./kubeconfig

# Check node status (should show Ready after ~7 minutes)
kubectl get nodes
# NAME            STATUS   ROLES           AGE     VERSION
# talos-iz8-auj   Ready    control-plane   3m36s   v1.32.0

# Check system pods
kubectl get pods -A
# All core components should be Running:
# - kube-apiserver
# - kube-controller-manager
# - kube-scheduler
# - kube-flannel (CNI)
# - kube-proxy
# - coredns (may take a few extra minutes to pull images)

# Check cluster info
kubectl cluster-info
# Kubernetes control plane is running at https://127.0.0.1:6443
# CoreDNS is running at https://127.0.0.1:6443/api/v1/namespaces/kube-system/services/kube-dns:dns/proxy
```

### 5. Single-Node Cluster Configuration

Since this is a single-node cluster acting as both control-plane and worker, remove the NoSchedule taint:

```bash
kubectl taint nodes --all node-role.kubernetes.io/control-plane:NoSchedule-
```

This allows workload pods to be scheduled on the control-plane node.

### 6. Test with Sample Application

```bash
# Deploy nginx
kubectl create deployment nginx --image=nginx:alpine
kubectl wait --for=condition=available deployment/nginx --timeout=5m

# Check deployment
kubectl get pods
kubectl exec deployment/nginx -- curl -s localhost | head -5
```

## Architecture

### Network Flow

```
Host Machine
├─ cloudflared (port 53) → DNS-over-HTTPS → Google DNS
├─ Python HTTPS Proxy (port 3128) → Authenticated Proxy → Internet
│
└─ QEMU VM (user-mode networking)
   ├─ IP: 10.0.2.15/24
   ├─ Gateway: 10.0.2.2 (host)
   ├─ DNS: 10.0.2.3 → forwards to 10.0.2.2:53 (cloudflared)
   ├─ Proxy: 10.0.2.2:3128 (Python HTTPS proxy)
   │
   └─ Talos Linux v1.9.2
      └─ Kubernetes v1.32.0
         ├─ API Server (forwarded to host :6443)
         └─ Talos API (forwarded to host :50000)
```

### Port Forwarding

QEMU forwards these ports from host to VM:
- **50000**: Talos API (for `talosctl` commands)
- **6443**: Kubernetes API (for `kubectl` commands)

### Component Stack

| Component | Version/Type | Purpose |
|-----------|-------------|---------|
| Host OS | Linux 4.4.0 | Container host |
| QEMU | 8.2.2 | VM hypervisor (no KVM) |
| Talos | v1.9.2 | Immutable OS |
| Kubernetes | v1.32.0 | Container orchestration |
| CNI | Flannel | Pod networking |
| DNS | CoreDNS | Cluster DNS |
| cloudflared | latest | DNS-over-HTTPS |
| HTTPS Proxy | Python 3 | Proxy forwarder with auth |

## Key Configuration Details

### Talos Configuration Patches

The terraform deployment applies these critical workarounds:

```yaml
machine:
  certSANs: ["127.0.0.1"]  # Allow API access via localhost

  time:
    disabled: true  # NTP unreliable, use QEMU RTC sync instead

  env:  # Proxy configuration for VM
    HTTP_PROXY: "http://10.0.2.2:3128"
    HTTPS_PROXY: "http://10.0.2.2:3128"
    NO_PROXY: "localhost,127.0.0.1,10.0.2.0/24"

  network:
    nameservers: ["10.0.2.3"]  # QEMU DNS (forwards to cloudflared)

  registries:  # Skip TLS verification (SSL interception)
    config:
      ghcr.io: { tls: { insecureSkipVerify: true } }
      gcr.io: { tls: { insecureSkipVerify: true } }
      registry.k8s.io: { tls: { insecureSkipVerify: true } }
      docker.io: { tls: { insecureSkipVerify: true } }

  install:
    disk: "/dev/vda"  # virtio disk
```

### QEMU Parameters

```bash
qemu-system-x86_64 \
  -name talos-qemu \
  -machine type=q35 \
  -cpu Nehalem \  # x86-64-v2 support (required by Talos v1.9.2)
  -m 2048 \  # 2GB RAM
  -smp 2 \  # 2 CPUs
  -drive file=talos-disk-tf.qcow2,if=virtio,format=qcow2 \
  -kernel vmlinuz-amd64 \
  -initrd initramfs-amd64.xz \
  -append "console=ttyS0 talos.platform=metal slab_nomerge pti=on" \  # KSPP params
  -netdev user,id=net0,hostfwd=tcp::50000-:50000,hostfwd=tcp::6443-:6443,dns=8.8.8.8 \
  -device virtio-net-pci,netdev=net0 \
  -rtc base=utc,clock=host \  # Clock sync (replaces NTP)
  -nographic
```

## Troubleshooting

### Check Proxy Processes

```bash
# Verify both proxies are running
ps aux | grep -E "(cloudflared|https-proxy)" | grep -v grep

# Check proxy logs
tail -f /tmp/cloudflared.log
tail -f /tmp/https-proxy.log
```

### Check VM Status

```bash
# Check if VM is running
ps aux | grep qemu | grep talos-qemu

# Check VM console output
tail -f /home/user/ducktape/talos-vm/vm-console-tf.log

# Look for errors
grep -i error /home/user/ducktape/talos-vm/vm-console-tf.log | tail -20
```

### Check Cluster Status

```bash
export KUBECONFIG=./kubeconfig

# Check connectivity
kubectl cluster-info

# Check node status
kubectl get nodes -o wide

# Check pod status
kubectl get pods -A

# Check specific pod logs
kubectl logs -n kube-system <pod-name>

# Describe pod for events
kubectl describe pod -n kube-system <pod-name>
```

### Common Issues

#### 1. DNS Resolution Failures

**Symptoms**: Logs show `i/o timeout` errors for DNS queries

**Solution**: Verify cloudflared is running and accessible
```bash
ps aux | grep cloudflared
dig @127.0.0.1 google.com
```

#### 2. Container Image Pull Failures

**Symptoms**: Pods stuck in `ContainerCreating`, image pull errors

**Solution**: Verify HTTPS proxy is running and has correct upstream proxy config
```bash
ps aux | grep https-proxy.py
echo $HTTPS_PROXY  # Should show authenticated proxy URL
tail /tmp/https-proxy.log
```

#### 3. Node Not Ready

**Symptoms**: Node shows `NotReady` status for >5 minutes

**Solution**: Check CNI (Flannel) pod status
```bash
kubectl get pods -n kube-system -l app=flannel
kubectl logs -n kube-system -l app=flannel
```

#### 4. CoreDNS Pods Pending

**Symptoms**: CoreDNS pods stuck in `Pending` or `ContainerCreating`

**Solution**: Wait for images to pull through proxy (can take 3-5 minutes). Check:
```bash
kubectl describe pod -n kube-system -l k8s-app=kube-dns
```

## Cleanup

### Destroy Cluster

```bash
cd terraform
terraform destroy -auto-approve
```

This will:
- Kill the QEMU VM
- Remove disk image
- Clean up terraform state

### Stop Proxy Services

```bash
# Stop cloudflared
kill $(cat /tmp/cloudflared.pid) 2>/dev/null || true

# Stop HTTPS proxy
kill $(cat /tmp/https-proxy.pid) 2>/dev/null || true

# Verify stopped
ps aux | grep -E "(cloudflared|https-proxy)" | grep -v grep
```

## Known Limitations

1. **NodePort Services**: Not accessible from host due to QEMU user-mode networking limitations
   - Use `kubectl port-forward` for testing
   - Or use `kubectl exec` to test from within pods

2. **External LoadBalancer**: Not supported (no MetalLB or cloud provider)

3. **Persistent Volumes**: Limited to VM disk only
   - No external storage integration
   - Data lost on `terraform destroy`

4. **Single Node**: No high availability
   - For HA, would need multiple VMs with bridged networking

5. **Performance**: No KVM acceleration
   - Slower than native/KVM deployments
   - CPU usage can be high during initialization

## Performance Characteristics

- **Boot time**: ~30 seconds to maintenance mode
- **Installation time**: ~2 minutes
- **Cluster bootstrap**: ~1 minute
- **Total deployment**: ~7 minutes
- **CPU usage**: High during boot (200%+), stabilizes to ~50% idle
- **Memory usage**: ~1.5-2GB actual usage from 2GB allocated

## Files and Directories

```
talos-vm/
├── README.md                 # Overview and comparison
├── DEPLOYMENT.md            # This file - complete deployment guide
├── terraform/
│   ├── main.tf              # Terraform configuration
│   ├── variables.tf         # Configurable parameters
│   ├── versions.tf          # Provider versions
│   ├── outputs.tf           # Output values
│   ├── kubeconfig          # Generated kubeconfig (after apply)
│   └── talosconfig         # Generated talosconfig (after apply)
├── manual/
│   ├── SETUP.md            # Manual setup guide
│   ├── https-proxy.py      # HTTPS proxy forwarder script
│   └── download-talos.sh   # Helper script
├── _out/                    # Downloaded Talos kernel/initramfs (gitignored)
├── talos-disk-tf.qcow2     # VM disk image (gitignored)
└── vm-console-tf.log       # VM console output (gitignored)
```

## References

- [Talos Documentation](https://www.talos.dev/v1.9/)
- [Talos Terraform Provider](https://registry.terraform.io/providers/siderolabs/talos/latest/docs)
- [QEMU User Networking](https://wiki.qemu.org/Documentation/Networking#User_Networking_(SLIRP))
- [Manual Setup Guide](../manual/SETUP.md) - Detailed step-by-step manual process

## Next Steps

Now that you have a working Talos Kubernetes cluster, you can:

1. **Deploy applications**: Use `kubectl create deployment` or apply manifests
2. **Explore Talos**: Use `talosctl` to interact with Talos OS directly
3. **Customize configuration**: Modify `terraform.tfvars` and re-apply
4. **Learn Kubernetes**: Use this as a local development/learning environment

## Success Criteria Checklist

- [ ] cloudflared process running on port 53
- [ ] https-proxy.py process running on port 3128
- [ ] QEMU VM process running
- [ ] `kubectl cluster-info` shows control plane running
- [ ] `kubectl get nodes` shows node in Ready status
- [ ] `kubectl get pods -A` shows all core system pods Running
- [ ] Can deploy and access test workload

All items checked = Fully operational cluster! ✅
