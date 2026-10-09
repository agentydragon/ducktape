# CiliumNetworkPolicy gotchas

## Gateway API backends must admit `reserved:ingress`

A default-deny `CiliumNetworkPolicy` in front of a pod that is the backend of a
Gateway API `HTTPRoute` (Cilium gateway implementation) must admit
`reserved:ingress` — **not** the gateway pod's namespace, **not** `reserved:host`,
**not** `reserved:remote-node`.

### Why

`cilium-envoy` runs `hostNetwork: true` on each node. Its egress to backend pods
uses the node's `cilium_host` interface IP, and Cilium assigns those interface
IPs the `reserved:ingress` identity — not `reserved:host`, not `reserved:remote-node`,
and not the `kube-system/cilium-envoy` pod identity. Any other selector drops the
SYN-ACK on the return path, so external requests through
`https://<host>.allegedly.works` return `503` even though the `HTTPRoute` is
`Accepted`, `ResolvedRefs=True`, the Service has endpoints, and the pod is
`Ready`.

### Pattern

```yaml
spec:
  endpointSelector:
    matchLabels:
      app.kubernetes.io/name: <backend>
  ingress:
    - fromEntities:
        - ingress
      toPorts:
        - ports:
            - port: "<facade-port>"
              protocol: TCP
```

Parked example: <../parked/manifold-mcp/networkpolicy.yaml>.

### Debugging a mis-classified source

1. `kubectl exec -n kube-system ds/cilium -- hubble observe --to-namespace <ns>` —
   SYN forwards reaching the pod with no return flow indicate a reverse-path
   policy drop.
2. `cilium ip get <source-ip>/32` from the destination node's cilium-agent
   reveals the identity Cilium assigned to that source.
3. Match the policy `fromEntities`/`fromEndpoints` to the actual identity.

Origin: manifold-mcp deployment (2026-04-30).

## Egress to a ClusterIP: allow the backend targetPort, not the Service port

A port-restricted egress rule to an in-cluster Service must list the backend
**`targetPort`**, not the Service `port`. With kube-proxy replacement, Cilium's
socket-LB rewrites `ClusterIP:port → podIP:targetPort` in the `connect()` hook —
**before** L4 egress policy is enforced — so the policy only ever sees the
translated backend port.

### Symptom

The client's TCP connection to the Service times out ("connection refused"/dial
timeout / `Client.Timeout ... while awaiting connection`), even though the egress
rule allows the Service's advertised port and DNS resolves fine. A pod with
unrestricted egress (all ports, or `toEntities: cluster` with no `toPorts`) reaches
the same Service without trouble — which is the tell that it's a port, not a
routing/DNS, problem.

### Example

`oci-cache`'s Service is `:80 → targetPort 5000`. Exposing it on `:80` did **not**
let `haku-ci`'s `toEntities: cluster` rule (ports `80/443/3000`) reach it — the
policy had to allow **5000**, the pod's container port. See
<../cdk8s/haku_ci/runner.py>.

### Debugging

`kubectl exec -n kube-system ds/cilium -- hubble observe --from-namespace <ns>
--type drop` shows the dropped egress flow with the actual (translated) destination
port — compare that to the ports your `toPorts` allows.

Origin: oci-cache pull-through mirror wiring (2026-07-04).

## Egress to `*.allegedly.works`: `world` excludes our own nodes

`toEntities: [world]` does not mean "any address outside this pod". Cilium carves
the cluster's own nodes out of `world` and gives them `reserved:remote-node`
(other nodes) or `reserved:host` (the node the pod runs on). Because the public
Gateway binds Envoy on the OVH nodes in `hostNetwork` mode, **every**
`*.allegedly.works` name resolves to those five node ExternalIPs — so a
`world`-only egress rule can reach the entire internet and still not reach any of
our own public services.

### Symptom: a hang, not a refusal

A dial that hangs for the client's full connect timeout and fails with `i/o
timeout` (not `connection refused`), to a host that resolves and that an
unrestricted pod on the same node reaches without trouble.

### Pattern: name the node entities

```yaml
- toEntities:
    - world
    - remote-node
    - host
  toPorts:
    - ports:
        - port: "443"
          protocol: TCP
```

`toEntities: [cluster]` also covers it — `cluster` expands to include
`remote-node` and `host` — and is why `haku-egress-proxy` reaches these IPs
today (`haku_cloud_api` in <../cdk8s/egress_fences.py>). Prefer
the narrower pair when the pod is deliberately barred from in-cluster pod-to-pod
egress.

**A `toFQDNs`/`toCIDR` rule cannot substitute.** `policy-cidr-match-mode` is unset
cluster-wide, so CIDR-derived selectors do not match node IPs at all — a
`toFQDNs: matchName: <something>.allegedly.works` rule looks correct, validates,
and still drops.

### Consequence: an FQDN allowlist cannot fence our own public services

An egress allowlist built from `toFQDNs` is a fence over the real internet only.
Every `*.allegedly.works` name is outside what it can express, so such a name
appearing in a `toFQDNs` block **grants nothing** — either a `toEntities` rule
elsewhere in the same policy is what actually permits it, or it does not work at
all. Both cases are live today; see the comments in <../cdk8s/egress_fences.py>.

The layer that _can_ fence these names is the DNS rule under
`toPorts.rules.dns`: the DNS proxy matches on the **query name** the pod sends,
before any identity is involved, so `matchName: haku-mailbox.allegedly.works`
there means exactly what it says. It bounds resolution rather than connection —
a pod that already holds the IP is unaffected — but for a proxy that resolves
what its clients ask for, that is the enforcement point.

### Debugging: check the destination's identity

`cilium-dbg ip get <ip>/32` (note: CIDR form, a bare IP is rejected) prints the
identity. `reserved:remote-node` on the destination against a `world`-only rule is
the whole diagnosis. A true-world address is simply absent from the ipcache.

Origin: public-coder-agent's Haku Console MCP server timing out at 30s while the
same proxy reached GitHub and BuildBuddy fine (2026-08-01).

## Egress through a local Gateway

A pod reaching its own node's Gateway through the public IP, Nebula IP, or Gateway
Service also needs egress permission for the **selected backend's** identity and
`targetPort`. Envoy retains the local source pod's policy (`cilium/bpf_metadata.cc`)
and checks it after backend selection (`cilium/l7policy.cc`), with the client's SNI.
Allowing `host`/`remote-node`:443 alone completes TLS but returns HTTP 403
(`server: envoy`, `Access denied`). A remote Gateway does not have that local source
pod policy, so the same request can succeed there (#9495).

Keep public-origin Agentplane `EgressPolicy` rules in sync with the shared backend
Cilium grants in `agentplane/egress_staging_credentials.py`. Reference the Service's selector and
**target** port: ActivityWatch's read Service maps 5600 to 5603, and Grocy's public
route targets Authentik on 9000. The proxy's application policy continues to scope
hosts, paths, methods and credentials. Validate HTTP from a restricted source pod;
a TLS-only probe or an unrestricted source does not exercise this check.

### Pattern: select the backend with the same SNI

Keep the node-entity rule and add the backend pods on their `targetPort` with the
same `serverNames` — the Authentik server pods on 9000 in
<../k8s/agentplane-staging/agentplane-staging.k8s.yaml>. Verify all three from the
client's network namespace: canonical SNI → 200, wrong SNI with the canonical
`Host` → 403, direct plaintext to `backend:9000` → reset (SNI scoping is intact).

### Gotchas

- The generated Service's EndpointSlice lists `192.192.192.192:9999`; that is a
  placeholder, not the datapath backend. `cilium-dbg service list` on the node
  shows the real local proxy backend, and a node the Gateway does not select has
  the Service entry with **no** backends: clients there fail with `EHOSTUNREACH`
  (errno 113), which is why <../terraform/main/cilium-values.yaml> selects every
  node for the Gateway.
- A TLS handshake succeeding through the Service proves nothing about the request:
  a wrong SNI completes TLS there and is only denied at HTTP.

Origin: agentplane-staging OIDC clients (2026-09-10, #6007, #6012).

## An SNI rule decides port 443 for every policy that selects the same Pod

`serverNames` is not a narrower line on the rule that carries it. It is an
L7 rule, and Cilium attaches L7 enforcement **per port on the endpoint**,
over the merge of every policy selecting that endpoint. So one policy
pinning SNI on 443 puts the endpoint's node-IP HTTPS behind an SNI match,
and the allowlist is the union of the `serverNames` lists in scope. Every
other policy selecting the same Pod — including one whose
`toEntities: [world, remote-node, host]:443` rule reads as fully open —
stops being open for any name nobody listed. Those connections reset
during the handshake, before the Gateway's listener sees a ClientHello.

Each rule is individually reasonable and the pair means something else
entirely, which is why this survives review: neither object mentions the
other.

### Symptom: one workload resets on node:443

Deterministic `Connection reset by peer` / `Connection reset during
handshake` from **one workload** to `*.allegedly.works` over node:443,
where the same dial from any other Pod succeeds. It presents as flapping:
a proxy pins one address per host for ~30 s and one node in six is the one
it runs on (`reserved:host`, admitted by an SNI-free rule), so a hard
per-name failure reads as ~1-in-6 successes. Only destinations whose sole
Cilium identity is `reserved:remote-node` are affected — `world`,
`reserved:host` and `reserved:kube-apiserver` traffic passes, so GitHub
and friends stay healthy and the failure looks like it belongs to the
destination.

### Pattern: open or pinned, one per endpoint and port

- Keep node:443 open and fence the names where they can actually be
  expressed: the DNS rule under `toPorts.rules.dns` (§ Consequence: an
  FQDN allowlist cannot fence our own public services), or the workload's
  own application-level allowlist, e.g. an agentplane `EgressPolicy`.
- Pin SNI on a **different port**, which carries its own L7 layer. Scoping
  Dex's `targetPort` narrows Dex and nothing else, which is how staging's
  proxy keeps both today.
- Pin it on a dedicated Pod identity that carries no open 443 rule.

`fleet_rules.https_egress_sni_conflicts` refuses the pair at synth, naming
both objects.

### Proof, from the incident

Same node, two Pods, one differing sibling policy.

- Staging's egress proxy: open `[world, remote-node, host]:443/80`,
  **plus a sibling pinning two names**. To a remote node's :443, those two
  names complete TLS and every other name — `www.allegedly.works`,
  `grocy-sf.allegedly.works`, the rest — resets.
- Testing's egress proxy: the identical open
  `[world, remote-node, host]:443/80`, no `serverNames` anywhere on its
  selector. 9/9 TLS 1.3 OK to the same three node IPs.
- Staging's Actions service: open 443 plus `serverNames` for `auth.`,
  `kubectl-passthrough-mcp.` and `grocy-mcp-sf.`. Exactly those three
  complete TLS; `www.allegedly.works` and
  `agentplane-testing.allegedly.works` reset — predicted from the policy
  before measured.

`cilium-dbg ip get` returns `reserved:remote-node` for every node
InternalIP and every OVH ExternalIP, so entity resolution was identical
for the passing and the failing destinations: the `serverNames` union was
the only variable. Staging's proxy CNP also holds an unrestricted
`toEntities: [kube-apiserver]`, which is why API traffic never broke.

### Debugging this

1. Dial from **inside the affected Pod** — an unpolicied Pod proves
   nothing — against a remote node's IP, including one name no policy
   should list, `www.allegedly.works`:

   ```bash
   python3 - "$NODE_IP" "$HOST" <<'EOF'
   import socket, ssl, sys
   sock = socket.create_connection((sys.argv[1], 443), 5)
   ssl.create_default_context().wrap_socket(sock, server_hostname=sys.argv[2]).do_handshake()
   print("TLS OK")
   EOF
   ```

   RST on the unlisted name with the listed ones succeeding is the whole
   diagnosis.

2. Read the `serverNames` of **every** policy selecting that Pod together
   (`kubectl get cnp -A -o yaml`, then match selectors): the answer is the
   union, not any one list.
3. Do not expect a policy drop from Hubble. `gatus` and `gateway-probe`
   dial node:443 from Pods carrying no policy, so
   `policy-verdict:none ALLOWED` beside a client-side reset means the
   probe is a bystander. A probe for this class has to run **as** the
   policed workload.

Onset was 2026-09-25 (#7911), which added a `serverNames`-bearing rule to
staging's egress proxy's own selector to reach the testing app's login
flow. That PR was itself a workaround for the own-node collision in
<../debug/agentplane_oidc/local_gateway_tls_rca.md>, since fixed by
`envoy.useOriginalSourceAddress: false`
(<../terraform/main/cilium-values.yaml>); the SNI rule outlived the thing
it worked around, and cost Grocy, Forgejo, Haku's mailbox, ActivityWatch
and aiquota for two weeks.

Origin: agentplane-staging sandboxes' Grocy/Forgejo/mailbox 502s
(2026-10-08).
