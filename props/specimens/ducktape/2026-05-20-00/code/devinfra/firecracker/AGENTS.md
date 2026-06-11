@README.md

## Agent Instructions

### Creating a VM

```bash
kubectl port-forward -n claude-sandbox svc/fc-manager 8080:8080 &
TOKEN=$(kubectl get secret -n claude-sandbox fc-manager-auth-token \
  -o jsonpath='{.data.token}' | base64 -d)
curl -s -H "Authorization: Bearer $TOKEN" localhost:8080/vms -XPOST \
  -d '{"cpus": 2, "mem_mib": 4096}'
```

### SSH into a VM

```bash
kubectl port-forward -n claude-sandbox pod/fc-vm-<id> 2222:22 &
ssh -p 2222 -o StrictHostKeyChecking=no root@localhost "<command>"
```

### Destroying a VM

```bash
curl -s -H "Authorization: Bearer $TOKEN" localhost:8080/vms/<id> -XDELETE
```
