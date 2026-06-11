# Nebula DNS DefaultRoute breaks host DNS during cluster bootstrapping

**Observed:** 2026-04-01, during `cluster-postmortem-bootstrap` session

## What happened

During cluster bootstrapping (VPS nodes being wiped and recreated), bash commands in
the Claude Code session kept timing out and going to background. The OTEL exporter in
the hook daemon also started logging DNS failures for `alloy-otlp.allegedly.works` — a
public internet hostname.

## Root cause

`nebula1` on `wyrm2` is configured with `+DefaultRoute` in systemd-resolved, with DNS
servers pointing at cluster nodes `10.42.0.{1,2,11,12}`:

```text
Link 3 (nebula1)
    Current Scopes: DNS LLMNR/IPv4 mDNS/IPv4
         Protocols: +DefaultRoute +LLMNR +mDNS ...
Current DNS Server: 10.42.0.12
       DNS Servers: 10.42.0.1 10.42.0.2 10.42.0.11 10.42.0.12
     Default Route: yes
```

When the VPS nodes were wiped during bootstrapping, Nebula lost connectivity to all
cluster nodes. Because `nebula1` is a default-route DNS interface, systemd-resolved
routed general DNS queries through the now-unreachable cluster DNS servers. This broke
**all** DNS on the host — not just cluster-internal names.

## Timeline

- **~18:35** — VPS nodes wiped; Nebula handshakes start timing out for all `10.42.0.*`
- **18:35:23** — Nebula logs DNS `i/o timeout` for its own `static_map` hosts
  (`talos-vps-*.nebula.allegedly.works`)
- **18:35–18:39** — `alloy-otlp.allegedly.works` still resolves intermittently
  (cached TTL or `ens18`'s `1.0.0.1` answering first)
- **18:39:27** — DNS fully broken for `alloy-otlp.allegedly.works`; hook daemon OTEL
  export switches from HTTP errors to `[Errno -2] Name or service not known`
- **18:39 onwards** — Every kubectl/flux/talosctl bash command blocks on TCP to dead
  k8s API (`5.78.106.249:6443`), hitting Claude Code's 120s timeout and going to
  background
- **21:31** — `wyrm2` rebooted; Nebula restarted with new working cluster nodes

`ens18` also has `+DefaultRoute` with DNS `1.0.0.1 1.1.1.1`, but systemd-resolved
was sending queries to the Nebula DNS servers first (or in parallel), and the i/o
timeouts from `10.42.0.*` caused resolution to fail before the `ens18` fallback could
answer.

## Fix Options

The goal is to keep short-name resolution for cluster nodes (e.g. `atlas`, `worker0`)
while not breaking public DNS when cluster nodes are down.

### Option 1: `networking.hosts` in NixOS (recommended)

Nebula IPs are **stable** — baked into node certs, not tied to VPS external IP. Add
them directly to `/etc/hosts` via NixOS config and remove DNS from `nebula1` entirely:

```nix
networking.hosts = {
  "10.42.0.1"  = ["talos-vps-cp-0"];
  "10.42.0.2"  = ["talos-vps-cp-1"];
  "10.42.0.10" = ["talos-pve-cp-0"];
  "10.42.0.11" = ["talos-vps-worker-0"];
  "10.42.0.12" = ["talos-vps-worker-1"];
  "10.42.0.20" = ["wyrm2"];
  # etc.
};
```

Remove `DefaultRoute = true` (and ideally all DNS servers) from the nebula1 interface
config. Public DNS goes through `ens18`'s `1.0.0.1` exclusively, with no cluster
dependency. Hosts file resolution is instant and requires no network at all.

**Downside**: the list must be kept in sync when nodes are added. For this cluster,
node additions always require a Nix config change anyway (joining the mesh), so this
is not a meaningful extra burden.

### Option 2: systemd-resolved routing domain (no DefaultRoute)

Remove `DefaultRoute` from `nebula1` and configure it with only a routing domain for
`~nebula.allegedly.works` (or whatever zone Nebula's DNS actually serves):

```ini
[Network]
DNS=10.42.0.1 10.42.0.2 10.42.0.11 10.42.0.12
Domains=~nebula.allegedly.works
```

This routes queries for `*.nebula.allegedly.works` to cluster DNS, and everything else
to `ens18`. Short bare names like `atlas` would **not** resolve via this path.

**Downside**: Nebula's built-in DNS has no option to serve names under a subdomain — it
only serves bare hostnames. So `atlas` would not resolve; you'd need to use a FQDN like
`atlas.nebula.allegedly.works` and configure CoreDNS to serve that zone. In practice
this is more work than option 1.

### Option 3: search domain + CoreDNS zone (does not work with Nebula DNS)

In theory, setting a search domain (e.g. `k`) on `nebula1` would let you type `atlas`
and have systemd-resolved try `atlas.k` against the cluster DNS. However:

- DNS queries are sent to the server with the suffix appended — the server receives
  `atlas.k`, not `atlas`.
- Nebula's DNS serves only bare names; it cannot serve `atlas.k`.
- CoreDNS would need an extra zone configured with `atlas.k` records.

This adds complexity with no benefit over option 1. **Not recommended.**

### Option 4: local DNS forwarder (dnsmasq/unbound)

Run a local resolver that has hardcoded A records for cluster nodes and forwards
everything else to `1.0.0.1`. systemd-resolved delegates to it. Gracefully handles
cluster DNS unavailability.

**Downside**: extra moving part, requires maintaining the same list as option 1. Only
worth it if you need more flexibility (e.g., automated record updates).

## Resolution

**Option 2** was implemented: `nebula1` uses routing domain `~nebula.allegedly.works`
(no `+DefaultRoute`). Only `*.nebula.allegedly.works` queries go to cluster DNS;
all other DNS goes through the host's default resolver. See `nix/nixos/modules/nebula.nix`.
