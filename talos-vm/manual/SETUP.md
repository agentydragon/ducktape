# Talos VM Success: Complete Working Solution

**Date**: 2025-11-17
**Status**: ✅ **COMPLETE** - kubectl working, HTTP server deployed

## Summary

Successfully created a Talos Linux v1.9.2 VM running Kubernetes v1.32.0 on QEMU 8.2.2 without KVM support, in an environment with SSL-intercepting proxy and networking restrictions. Achieved working kubectl access and deployed a functioning HTTP server to the cluster.

## Final Working State

```bash
$ kubectl --kubeconfig=kubeconfig-talos get nodes
NAME            STATUS   ROLES           AGE   VERSION
talos-j0o-ms7   Ready    control-plane   2m    v1.32.0

$ kubectl --kubeconfig=kubeconfig-talos get pods
NAME                     READY   STATUS    RESTARTS   AGE
nginx-6b66fbbd46-tknvp   1/1     Running   0          2m

$ kubectl --kubeconfig=kubeconfig-talos exec deployment/nginx -- wget -O- -q localhost | head -3
<!DOCTYPE html>
<html>
<head>
```

## Architecture

### Network Flow

```
VM (10.0.2.15)
  ├─ DNS: 10.0.2.3 (QEMU DNS) → 10.0.2.2:53 (cloudflared DoH) → Google DoH
  └─ Proxy: 10.0.2.2:3128 (Python proxy) → 21.0.0.103:15004 (JWT auth) → Internet
```

### Component Stack

- **Host**: QEMU 8.2.2 (no KVM)
- **Guest OS**: Talos Linux v1.9.2
- **Kubernetes**: v1.32.0
- **CNI**: Flannel
- **DNS**: cloudflared DNS-over-HTTPS
- **Proxy**: Python CONNECT proxy with JWT auth

## Complete Solution Steps

### 1. Environment Discovery

Discovered environment constraints:
- HTTPS_PROXY at `21.0.0.103:15004` with JWT authentication
- SSL/TLS interception with custom CA: "Anthropic sandbox-egress-production TLS Inspection CA"
- UDP traffic (DNS, NTP) unreliable through QEMU user-mode networking

### 2. QEMU VM Setup

**File**: `start-vm-kernel.sh`

```bash
#!/bin/bash
KERNEL="$PWD/_out/vmlinuz-amd64"
INITRD="$PWD/_out/initramfs-amd64.xz"
DISK="$PWD/talos-disk.qcow2"

exec qemu-system-x86_64 \
  -name talos-kernel \
  -machine type=q35 \
  -cpu Nehalem \  # x86-64-v2 support
  -m 2048 \
  -smp 2 \
  -drive file=$DISK,if=virtio,format=qcow2 \
  -kernel $KERNEL \
  -initrd $INITRD \
  -append "console=ttyS0 talos.platform=metal slab_nomerge pti=on" \  # KSPP params
  -netdev user,id=net0,hostfwd=tcp::50000-:50000,hostfwd=tcp::6443-:6443,dns=8.8.8.8 \
  -device virtio-net-pci,netdev=net0 \
  -rtc base=utc,clock=host \  # Clock sync fix
  -nographic
```

**Key Fixes**:
- `Nehalem` CPU for x86-64-v2 support (Talos v1.9.2 requirement)
- KSPP kernel parameters (`slab_nomerge pti=on`)
- virtio disk (`/dev/vda`)
- RTC clock sync to avoid NTP dependency
- Port forwarding for Talos API (50000) and K8s API (6443)

### 3. DNS Solution

**Issue**: UDP DNS port 53 unreliable through QEMU user-mode networking

**Solution**: cloudflared DNS-over-HTTPS proxy

```bash
# On host
nohup /tmp/cloudflared proxy-dns --address 0.0.0.0 --port 53 \
  --upstream https://dns.google/dns-query > /tmp/cloudflared.log 2>&1 &
echo "nameserver 127.0.0.1" > /etc/resolv.conf
```

### 4. Proxy Solution

**Issue**: Environment proxy requires JWT authentication in HTTP headers

**Solution**: Python CONNECT proxy forwarder

**File**: `https-proxy.py`

```python
#!/usr/bin/env python3
"""
HTTP CONNECT proxy that forwards to an upstream authenticated proxy.
Used to allow QEMU VM (without auth) to connect through the environment's authenticated proxy.
"""
import socket
import select
import threading
import sys
import os
from urllib.parse import urlparse

LISTEN_HOST = '0.0.0.0'
LISTEN_PORT = 3128
BUFFER_SIZE = 8192

# Get upstream proxy from environment
UPSTREAM_PROXY = os.getenv('HTTPS_PROXY', os.getenv('HTTP_PROXY', ''))
parsed = urlparse(UPSTREAM_PROXY)
UPSTREAM_HOST = parsed.hostname
UPSTREAM_PORT = parsed.port or 80
UPSTREAM_AUTH = f"{parsed.username}:{parsed.password}" if parsed.username and parsed.password else None

def handle_client(client_socket, client_address):
    upstream_socket = None
    try:
        # Read CONNECT request from client
        request = client_socket.recv(BUFFER_SIZE).decode('utf-8')
        parts = request.split(' ')
        if len(parts) < 2 or parts[0] != 'CONNECT':
            print(f"Invalid request from {client_address}")
            return

        target = parts[1]  # e.g., "ghcr.io:443"
        print(f"Client wants to connect to {target}")

        # Connect to upstream proxy
        upstream_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream_socket.connect((UPSTREAM_HOST, UPSTREAM_PORT))

        # Send CONNECT with authentication
        upstream_request = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
        if UPSTREAM_AUTH:
            import base64
            auth_b64 = base64.b64encode(UPSTREAM_AUTH.encode()).decode()
            upstream_request += f"Proxy-Authorization: Basic {auth_b64}\r\n"
        upstream_request += "\r\n"

        print(f"Sending CONNECT to upstream (auth: {'yes' if UPSTREAM_AUTH else 'no'})")
        upstream_socket.sendall(upstream_request.encode())

        # Check upstream response
        upstream_response = b''
        while b'\r\n\r\n' not in upstream_response:
            chunk = upstream_socket.recv(BUFFER_SIZE)
            if not chunk:
                print(f"Upstream closed connection before sending response")
                return
            upstream_response += chunk

        # Parse response
        response_line = upstream_response.split(b'\r\n')[0].decode()
        if '200' not in response_line:
            print(f"Upstream proxy rejected connection: {response_line}")
            client_socket.sendall(upstream_response)
            return

        print(f"Connection established through upstream proxy to {target}")

        # Forward success to client
        client_socket.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')

        # Bi-directional forwarding
        client_socket.setblocking(False)
        upstream_socket.setblocking(False)

        while True:
            read_sockets, _, _ = select.select([client_socket, upstream_socket], [], [], 1.0)

            for sock in read_sockets:
                data = sock.recv(BUFFER_SIZE)
                if not data:
                    return

                if sock is client_socket:
                    upstream_socket.sendall(data)
                else:
                    client_socket.sendall(data)

    except Exception as e:
        print(f"Error handling {client_address}: {e}")
    finally:
        client_socket.close()
        if upstream_socket:
            upstream_socket.close()

def main():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((LISTEN_HOST, LISTEN_PORT))
    server.listen(5)

    print(f"Proxy listening on {LISTEN_HOST}:{LISTEN_PORT}")
    print(f"Forwarding to upstream: {UPSTREAM_HOST}:{UPSTREAM_PORT}")

    try:
        while True:
            client, addr = server.accept()
            print(f"Accepted connection from {addr}")
            thread = threading.Thread(target=handle_client, args=(client, addr))
            thread.daemon = True
            thread.start()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        server.close()

if __name__ == '__main__':
    main()
```

**Start proxy**:
```bash
nohup python3 ./https-proxy.py > /tmp/python-proxy.log 2>&1 &
```

### 5. TLS Certificate Verification Solution

**Issue**: SSL-intercepting proxy causes certificate validation failures

**Workaround**: Use `insecureSkipVerify` for all container registries (clock skew made CA trust insufficient)

**File**: `controlplane.yaml` (key sections)

```yaml
machine:
  certSANs:
    - 127.0.0.1  # Critical for talosctl to connect via localhost

  time:
    disabled: true  # NTP blocked, rely on QEMU RTC sync

  env:
    HTTP_PROXY: http://10.0.2.2:3128
    HTTPS_PROXY: http://10.0.2.2:3128
    NO_PROXY: localhost,127.0.0.1,10.0.2.0/24

  network:
    nameservers:
      - 10.0.2.3  # QEMU DNS → host cloudflared

  install:
    disk: /dev/vda  # virtio disk
    image: ghcr.io/siderolabs/installer:v1.9.2

  registries:
    config:
      ghcr.io:
        tls:
          insecureSkipVerify: true
      gcr.io:
        tls:
          insecureSkipVerify: true
      registry.k8s.io:
        tls:
          insecureSkipVerify: true
      docker.io:
        tls:
          insecureSkipVerify: true
```

### 6. Installation Steps

**Start services**:
```bash
# 1. Start DNS-over-HTTPS
nohup /tmp/cloudflared proxy-dns --address 0.0.0.0 --port 53 \
  --upstream https://dns.google/dns-query > /tmp/cloudflared.log 2>&1 &

# 2. Start authenticated proxy
nohup python3 ./https-proxy.py > /tmp/python-proxy.log 2>&1 &

# 3. Create disk image
qemu-img create -f qcow2 talos-disk.qcow2 20G

# 4. Start VM
nohup ./start-vm-kernel.sh > vm-console.log 2>&1 &

# 5. Wait for maintenance mode (20-30 seconds)
sleep 25

# 6. Apply configuration (CRITICAL: must be done BEFORE first boot completes)
./talosctl apply-config --talosconfig=talosconfig --nodes 127.0.0.1 \
  --file controlplane.yaml --insecure
```

**Wait for installation** (60-90 seconds):
```bash
# Monitor installation
tail -f vm-console.log | grep -E "install|success"

# Monitor proxy activity
tail -f /tmp/python-proxy.log
```

**After reboot, bootstrap Kubernetes**:
```bash
# Wait for etcd to be ready (look for "etcd is waiting to join the cluster")
./talosctl --talosconfig=talosconfig --nodes 127.0.0.1 dmesg | grep "etcd is waiting"

# Bootstrap the cluster
./talosctl --talosconfig=talosconfig --nodes 127.0.0.1 bootstrap

# Wait for services to start (30-60 seconds)
./talosctl --talosconfig=talosconfig --nodes 127.0.0.1 services

# Generate kubeconfig
./talosctl kubeconfig kubeconfig-talos --talosconfig=talosconfig --nodes 127.0.0.1

# Verify kubectl
kubectl --kubeconfig=kubeconfig-talos get nodes
```

### 7. Deploy HTTP Server

```bash
# Deploy nginx
kubectl --kubeconfig=kubeconfig-talos create deployment nginx --image=nginx:alpine
kubectl --kubeconfig=kubeconfig-talos expose deployment nginx --port=80 --type=NodePort

# Remove control-plane taint (single-node cluster)
kubectl --kubeconfig=kubeconfig-talos taint nodes --all \
  node-role.kubernetes.io/control-plane:NoSchedule-

# Wait for pod (may take 1-2 minutes to pull image)
kubectl --kubeconfig=kubeconfig-talos get pods -w

# Test HTTP server
kubectl --kubeconfig=kubeconfig-talos exec deployment/nginx -- wget -O- -q localhost
```

## Critical Configuration Points

### 1. Certificate SANs

**MUST** include `127.0.0.1` in `machine.certSANs` for talosctl to connect via localhost port forwarding.

### 2. Time Sync

**MUST** disable Talos time sync (`machine.time.disabled: true`) and rely on QEMU RTC sync, as NTP is blocked.

### 3. Registry TLS

**MUST** configure `insecureSkipVerify: true` for ALL registries used:
- `ghcr.io` - Talos installer
- `gcr.io` - etcd
- `registry.k8s.io` - Kubernetes components
- `docker.io` - Application images

### 4. Configuration Timing

**CRITICAL**: Apply configuration in maintenance mode BEFORE the first boot completes. If applied after installation, certificates are regenerated incorrectly.

## Troubleshooting

### Check Service Status
```bash
./talosctl --talosconfig=talosconfig --nodes 127.0.0.1 services
```

### View Logs
```bash
# System logs
./talosctl --talosconfig=talosconfig --nodes 127.0.0.1 dmesg

# Specific service
./talosctl --talosconfig=talosconfig --nodes 127.0.0.1 logs kubelet

# Proxy activity
tail -f /tmp/python-proxy.log

# DNS activity
tail -f /tmp/cloudflared.log

# VM console
tail -f vm-console.log
```

### Check Connectivity
```bash
# Test talosctl connection
./talosctl --talosconfig=talosconfig --nodes 127.0.0.1 version

# Test kubectl
kubectl --kubeconfig=kubeconfig-talos get nodes

# Check network
./talosctl --talosconfig=talosconfig --nodes 127.0.0.1 get resolvers
```

## Performance Notes

- **Image pulls**: 30-90 seconds each (via SSL-intercepting proxy)
- **Installation**: ~90 seconds total
- **Bootstrap**: ~60 seconds
- **CNI ready**: ~60 seconds after bootstrap
- **Total setup**: ~5-6 minutes from VM start to working kubectl

## Known Limitations

1. **No NTP**: Clock sync via QEMU RTC only
2. **No DNS resolution for hostnames**: UDP DNS unreliable, only DNS-over-HTTPS works
3. **NodePort services**: Not accessible from host (only 50000, 6443 forwarded)
4. **Single-node**: Control plane must allow workload scheduling
5. **TLS security reduced**: `insecureSkipVerify` required for SSL-intercepting proxy

## Files Created

### Scripts
- `start-vm-kernel.sh` - QEMU startup with all fixes
- `https-proxy.py` - Authenticated proxy forwarder
- `download-talos.sh` - Component downloader

### Configuration
- `controlplane.yaml` - Complete Talos configuration
- `talosconfig` - Talos client configuration (generated by talosctl gen config)
- `kubeconfig-talos` - Kubernetes client configuration

### Logs
- `vm-console.log` - QEMU console output
- `/tmp/python-proxy.log` - Proxy activity
- `/tmp/cloudflared.log` - DNS-over-HTTPS activity

### Documentation
- `SUCCESS.md` - This file
- `TASK.md` - Task tracking
- `SUMMARY.md` - Project overview
- `PROXY-SOLUTION.md` - Detailed proxy solution
- `DNS-SOLUTION.md` - DNS workaround details

## Next Steps: Terraform Automation

See `TERRAFORM.md` for implementing this solution with Terraform using:
- Talos Image Factory for pre-baked configuration
- libvirt Terraform provider
- Automated bootstrap and health checks

## References

- [Talos Linux v1.9 Documentation](https://www.talos.dev/v1.9/)
- [Talos Image Factory](https://factory.talos.dev/)
- [QEMU Documentation](https://www.qemu.org/documentation/)
- [cloudflared DNS-over-HTTPS](https://developers.cloudflare.com/cloudflare-one/connections/connect-devices/agentless/dns/dns-over-https/)

---

**Achievement Unlocked**: ✅ Working Kubernetes cluster with functioning kubectl and HTTP server deployment, despite SSL-intercepting proxy, no KVM, and networking restrictions!
