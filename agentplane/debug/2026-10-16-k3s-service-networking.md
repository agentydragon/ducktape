# k3s Service networking on the RBE Firecracker executor

## Constraint

The current Firecracker guest does not expose the iptables `MARK` target required by kube-proxy.
Keep the fixture's headless Services, node-IP Kubernetes API access and host-network CoreDNS until
an executor supports ordinary Service routing. This workaround limits coverage: passing lifecycle
tests do not establish ClusterIP routing, Service port translation or ordinary in-cluster API access.

The kernel belongs to the execution environment. Adding iptables packages to
`devinfra/rbe_container_image/Dockerfile` or repinning its image cannot add a guest-kernel feature.
The repo-wide `rbe_linux_x64` platform selects Firecracker; k3s test properties inherit that selection.

## Failed attempt and evidence

[#10808](https://github.com/agentydragon/ducktape/pull/10808) enabled bundled kube-proxy, changed
fixture Services to ClusterIP and removed the node-IP API and DNS compensations. It retained
Flannel `host-gw`, the Pod-CIDR masquerade rule and the sysctl shadow required by this executor.

The uncached [real lifecycle invocation](https://app.buildbuddy.io/invocation/060dcde2-ae18-4413-a87b-1fe8b5272a36)
failed. It ran the networking patch on base `8ca2e63f`, before the PR was rebased onto the other
bootstrap improvements. It is evidence against that networking attempt, not an exact-head run of
merge commit `cc363baee732b98228324eb0cac246f21cd0fa30`.

- Manifest application reported resources created; no rejected manifest was found.
- `k3s.log` identifies K3s `v1.33.4+k3s1` and `Using iptables Proxier`.
- At `2026-10-17T01:11:26Z`, kube-proxy failed `iptables-restore` with the diagnostic below. The
  failure repeated, and Flannel's rule setup reported the same unsupported mark extension.
- CoreDNS remained unready, requests to DNS Service `10.43.0.10:53` were refused, and controllers
  could not reach API Service `10.43.0.1:443`. Fixture Deployments never became Available, so the
  newly added in-Pod Service/DNS assertion never ran.
- A separate read-only probe on the same pinned action image with Firecracker isolation reported
  kernel `5.15.0`, `iptables-restore` v1.8.10 using the **legacy** backend, and no `MARK` target in
  `/proc/net/ip_tables_targets`. Switching away from an accidental nftables backend therefore does
  not explain this failure. The missing target and rule-installation errors identify the kernel
  capability boundary; the probe did not establish an alternative working executor.

```text
Failed to execute iptables-restore
Warning: Extension mark revision 0 not supported, missing kernel module?
iptables-restore: line 12 failed
```

Retrieve the retained runtime evidence with the repo's `bbapi` tool:

```bash
bbapi target log 060dcde2-ae18-4413-a87b-1fe8b5272a36 \
  //agentplane/x/k3s_env:test_lifecycle --artifact test.outputs/k3s.log
```

Library builds, render tests and required PR CI passed for the networking change. They did not
exercise this failure: the real lifecycle target is manual and excluded from regular PR CI. A
green render/build result is insufficient evidence for removing executor workarounds.

## Revert scope and validation boundary

The corrective change reverts #10808, including its Service-routing assertion, because the
restored fixture deliberately bypasses ClusterIP routing. It retains checked manifest application
with full command diagnostics, bounded bootstrap probes and k3s-exit detection, and removal of fake
KubeVirt CRDs. No mock k3s tests or custom startup scheduler are introduced.

The preceding fixture has real lifecycle pass evidence, including
[the bounded-bootstrap run](https://app.buildbuddy.io/invocation/098d4dd5-76ec-480f-a583-23ffafce4c23)
and [the fixture without fake KubeVirt CRDs](https://app.buildbuddy.io/invocation/70241648-77df-43d9-aeab-f5f27e1afcd1).
These are prior runs, not acceptance of the corrective PR's exact revision. The revert has not
been given a new real lifecycle run; its PR reports current CI separately.

## Conditions for retrying standard networking

1. On a candidate executor, first use a small remote action to verify actual insertion/removal of
   the mark match/target rules kube-proxy needs, plus the namespace, mount and network privileges
   required by k3s. Module filenames or a different container image alone are insufficient.
2. If the hosted guest cannot supply these capabilities, investigate a BuildBuddy guest-kernel
   change or a suitable RBE pool. Keep executor selection scoped to the k3s test. An alternative
   isolation mode has not yet been validated for this fixture's privilege requirements.
3. Run the full real lifecycle target with kube-proxy enabled. Require ready DNS/storage/service
   Deployments and an in-Pod probe that resolves `kubernetes.default.svc.cluster.local`, observes
   the Service IP and reaches the authenticated API through Service port `443` to target `6443`.
   Preserve the existing lifecycle assertions and require this evidence before merging.

The intended setup remains images plus declarative manifests, Kubernetes reconciliation,
readiness, then tests. Increasing readiness timeouts or manually sequencing dependent Deployments
cannot supply missing kernel functionality.
