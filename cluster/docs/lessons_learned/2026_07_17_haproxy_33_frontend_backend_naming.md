# haproxy 3.3 breaks same-named frontend/backend → NixOS workers NotReady

- **Date:** 2026-07-17
- **Trigger:** a NixOS `nixos-rebuild switch` that pulled haproxy 3.3.x

## Symptom

`wyrm2` (and any NixOS k8s worker) shows `NotReady` in `kubectl get nodes`,
condition `Ready=Unknown`, `Reason=NodeStatusUnknown`, message "Kubelet stopped
posting node status." Pods on the node sit `Pending`; scheduled GPU workloads
(Ollama, vLLM) never come up.

## Root cause

The node's local kube-apiserver load balancer is haproxy on `127.0.0.1:7445`
(<../../../nix/nixos/modules/k8s-worker.nix>). `kubelet.service` has
`Requires=haproxy.service`, so if haproxy can't start, kubelet doesn't either.

haproxy **3.3** stopped allowing a `frontend` and a `backend` to share a name:

```text
[ALERT] config : Parsing [...:18]: backend 'kube-apiserver' has the same name as
frontend 'kube-apiserver' declared at [...:14]. This is no longer supported as of
3.3. Please rename one or the other.
```

The module declared both `frontend kube-apiserver` and `backend kube-apiserver`.
That was valid on haproxy < 3.3; the nixpkgs bump to 3.3.x made the generated
`haproxy.conf` invalid, so `ExecStartPre=haproxy -c` failed, systemd hit the
restart start-limit, and haproxy stayed `failed`.

## Fix

Rename the backend so the names differ (`kube-apiserver-backend`), keeping the
frontend name. One-node scope isn't enough — the module is shared by every
NixOS worker (`wyrm2`, `iguana`, `rugged`), so all of them regress on the next
switch until this lands.

Validate before switching a node by running the real binary against the
generated config on the host:

```bash
haproxy -c -f /nix/store/<hash>-haproxy.conf   # exit 0 = valid
```

## Recovery on an already-broken node

The fix is declarative; apply it by rebuilding from the corrected config
(`nixos-rebuild switch`), then confirm:

```bash
systemctl is-active haproxy kubelet     # both active
kubectl get node <name>                 # Ready
```

Do not hand-patch the running `haproxy.conf` in `/nix/store` — the next switch
regenerates it and re-breaks the node.
