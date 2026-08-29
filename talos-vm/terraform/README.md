# Talos Kubernetes on QEMU (Terraform)

Automated deployment of Talos Linux v1.9.2 Kubernetes cluster on QEMU.

**Status**: ✅ Tested end-to-end, deploys working cluster in ~5-6 minutes.

## Quick Start

```bash
# 1. Start required services (if needed)
nohup /tmp/cloudflared proxy-dns --port 53 --upstream https://dns.google/dns-query &
nohup python3 ../manual/https-proxy.py &

# 2. Deploy cluster
terraform init
terraform apply

# 3. Access cluster (~5min later)
export KUBECONFIG=./kubeconfig
kubectl get nodes

# 4. Remove control-plane taint (single-node)
kubectl taint nodes --all node-role.kubernetes.io/control-plane:NoSchedule-

# 5. Cleanup when done
terraform destroy
```

## Requirements

- Terraform >= 1.5.0
- QEMU (qemu-system-x86_64)
- kubectl

**Providers** (auto-downloaded):
- siderolabs/talos (~> 0.7.0)
- hashicorp/null (~> 3.2.0)
- hashicorp/local (~> 2.5.0)

## Configuration

Customize via `terraform.tfvars`:

```hcl
cluster_name       = "my-cluster"
vm_memory          = 4096  # MB
vm_cpus            = 4
talos_version      = "v1.9.2"
kubernetes_version = "v1.32.0"
```

See `variables.tf` for all options.

## Known Limitations

**NodePort not accessible from host**: QEMU user-mode networking only forwards ports 50000 (Talos API) and 6443 (K8s API).

Test services using:
```bash
kubectl port-forward deployment/nginx 8080:80
# or
kubectl exec deployment/nginx -- curl localhost
```

## Troubleshooting

**VM not starting:**
```bash
tail -f ../manual/vm-console-tf.log
ps aux | grep qemu | grep talos-qemu
```

**Image pulls failing:**
- Check proxy is running: `ps aux | grep https-proxy.py`
- Check DNS: `ps aux | grep cloudflared`

**Cluster not ready:**
```bash
export KUBECONFIG=./kubeconfig
kubectl get pods -A
kubectl get nodes
```

## Technical Details

Uses `null_resource` with direct QEMU commands (no libvirt dependency). All Talos configuration workarounds from [`../manual/SETUP.md`](../manual/SETUP.md) are automated.

**Key fix**: Used `abspath()` for path resolution to avoid relative path issues.

## References

- [Talos Documentation](https://www.talos.dev/v1.9/)
- [Talos Terraform Provider](https://registry.terraform.io/providers/siderolabs/talos/latest/docs)
- [Manual Setup Guide](../manual/SETUP.md)
