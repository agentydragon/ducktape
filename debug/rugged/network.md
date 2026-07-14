# Rugged network connectivity investigations

This note keeps **observed** path failures separate from configuration changes.
Do not change a NetworkManager profile or disconnect a link merely to reproduce
an issue: record the baseline first, then make an explicitly approved,
reversible experiment.

## GitHub SSH delay with Wi-Fi and Google Fi connected (2026-07-13)

### Symptom

`git fetch`, `git push`, and `git ls-remote` sometimes pause for many seconds
before doing useful work. When it happens, Git is waiting for the SSH TCP
connection, not for hooks, pack transfer, or GitHub ref negotiation.

### Baseline: both links connected

| Item                          | Observed state                                            |
| ----------------------------- | --------------------------------------------------------- |
| Wi-Fi (`wlp0s20f3`)           | Default IPv4 and IPv6 route, metric 600                   |
| Google Fi (`wwan0`)           | IPv4 default route, metric 1050; IPv6 disabled            |
| Wi-Fi resolver                | `github.com` has A, but no AAAA record                    |
| Google Fi resolver            | Returns DNS64 AAAA `64:ff9b::8c52:7403` for `github.com`  |
| Normal address ordering       | The DNS64 IPv6 address comes before GitHub's IPv4 address |
| Route to `64:ff9b::8c52:7403` | Wi-Fi IPv6 default route, not WWAN                        |

The resulting path is therefore mixed: WWAN DNS supplies an IPv6/NAT64
answer, but the kernel sends its connection over Wi-Fi. That IPv6 path timed
out in the baseline.

`wwan0` is the IP network interface. `wwan0mbim0` is the modem's MBIM control
port, which is why ModemManager and NetworkManager output can mention both
names. Use `wwan0` when binding an IP connection to the cellular link.

DNS64 is an IPv6-to-IPv4 compatibility service, not evidence that Google Fi
lacks IPv4. The resolver receives a request for an AAAA record, looks up the
ordinary A record when the destination has no real AAAA record, and embeds that
IPv4 address in the `64:ff9b::/96` well-known NAT64 prefix. In this case,
`64:ff9b::8c52:7403` embeds `140.82.116.3`, a GitHub IPv4 address. A correctly
matched NAT64 gateway would translate packets sent to that IPv6 address back to
the embedded IPv4 destination. Google Fi's IPv4 address, default route, and A
record resolution were all present in the baseline. The failure is that this
host receives Fi's synthesized answer while selecting Wi-Fi's unrelated IPv6
egress, not an absence of IPv4 service.

### Controlled probes

All results below were collected on 2026-07-13 with both links up.

| Probe                                                    | Result                                  | Interpretation                                     |
| -------------------------------------------------------- | --------------------------------------- | -------------------------------------------------- |
| Wi-Fi-bound `ssh` to `github.com:22`                     | Authenticated in 0.91 s                 | Wi-Fi GitHub SSH is healthy                        |
| Wi-Fi-bound `ssh` to `ssh.github.com:443`                | Reached host-key verification in 0.33 s | GitHub's SSH-over-443 endpoint is healthy on Wi-Fi |
| Fi-bound HTTPS to `www.google.com/generate_204`          | HTTP 204 in 0.46 s                      | Fi IPv4 HTTPS works                                |
| Fi-bound HTTPS to `github.com`                           | HTTP 200 in 0.84 s                      | Fi IPv4 HTTPS works                                |
| Fi-bound `ssh` to `github.com:22`                        | TCP connection timed out after 7 s      | Current Fi path cannot reach normal GitHub SSH     |
| Fi-bound `ssh` to `ssh.github.com:443`                   | TCP connection timed out after 7 s      | GitHub SSH-over-443 also fails over Fi             |
| Fi-bound Cloudflare DNS-over-HTTPS query for GitHub AAAA | Valid NODATA response (no AAAA answer)  | A public resolver avoids Fi's synthetic AAAA       |
| `nc -6 -z -w 7 64:ff9b::8c52:7403 {22,443}` via Wi-Fi    | Timed out                               | The selected IPv6/NAT64 path is unusable           |

This distinguishes two problems that can look alike:

1. Google Fi IPv4 supports ordinary HTTPS, but its current path to both
   GitHub SSH endpoints times out. This is independent of the DNS64 issue.
2. When both links are active, Google Fi's DNS64 answer can make applications
   prefer an IPv6 address that is actually routed through Wi-Fi and also fails.

The second problem explains intermittent Git SSH setup delays while Wi-Fi is
the preferred route: connection attempts can wait for the bad IPv6 path before
falling back to Wi-Fi IPv4. It does **not** show that WWAN carries that failed
IPv6 traffic.

### Reproduction commands (read-only)

Use the current link names and source addresses from `ip addr`; they may change
after reconnecting. `-b <source-address>` alone does **not** isolate egress in
this host's route table: while Wi-Fi has the better default route, a socket
bound to Fi's source address is still sent to the Wi-Fi gateway. Bind the
network interface instead.

```bash
# Compare each resolver without using resolved's cache.
resolvectl --cache=no -i wlp0s20f3 -6 -t AAAA query github.com
resolvectl --cache=no -i wwan0 -6 -t AAAA query github.com

# Show the addresses normal applications will try and the selected route.
getent ahosts github.com
ip -6 route get 64:ff9b::8c52:7403

# Device-bound probes isolate egress correctly.
curl -4 --interface if!wwan0 -I https://www.google.com/generate_204
ssh -4 -o BindInterface=wwan0 -o BatchMode=yes -o ConnectTimeout=7 \
  -T git@github.com
```

For a privileged, packet-level check of the two Fi-bound SSH attempts without
altering any network state, run:

```bash
bash debug/rugged/fi-network-diagnose.sh
```

It prompts for `sudo` only to capture TCP headers for the two GitHub endpoints.
The capture distinguishes a local failure from SYN packets that leave rugged
without a reply.

### Deployed DNS stopgap (2026-07-13)

The selected mitigation is an explicit higher `ipv4.dns-priority = 200` on the
declarative Google Fi profile. NetworkManager's `dns=systemd-resolved`
integration uses that priority to select between equal `~.` DNS routing domains;
the lower numerical priority wins. This leaves the existing IPv4 route metrics
unchanged: Wi-Fi DNS wins when Wi-Fi is connected, and WWAN DNS remains usable
when it is the only active link. It must take effect through the NixOS profile,
not an imperative `nmcli` edit.

Post-activation, with both links connected, the active profile and actual
GitHub path were verified as follows:

| Check                                                                   | Observed result                                                                        |
| ----------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `nmcli -g ipv4.dns-priority connection show 'Google Fi'`                | `200`                                                                                  |
| `resolvectl --cache=no query github.com`                                | Wi-Fi link, GitHub IPv4 A record only                                                  |
| `getent ahosts github.com`                                              | No `64:ff9b::/96` synthetic IPv6 address                                               |
| `git ls-remote --symref origin HEAD`                                    | Completed in 1.53 s                                                                    |
| Five fresh SSH handshakes with multiplexing and authentication disabled | Reached GitHub in 0.49–0.63 s each; expected `Permission denied (publickey)` afterward |

The last row deliberately does not use an existing SSH control socket or an
SSH key. It proves a fresh TCP and SSH handshake completed quickly; the auth
failure is expected. This validates the fix for the recurring **Wi-Fi + Fi**
Git setup delay.

Do not treat a successful NixOS rebuild alone as proof that this setting is
live. A switch from a source tree that lacked this declaration left the active
profile at priority `0`. Always check the first command in the table after
activation.

### What this stopgap does not fix

This is intentionally not the full Google Fi solution.

1. **Fi-only Git SSH is still broken.** Direct, device-bound Fi IPv4 HTTPS to
   GitHub works, but Fi-bound SSH to both `github.com:22` and
   `ssh.github.com:443` timed out. The latter means merely moving SSH to port
   443 does not currently provide a Fi fallback.
2. **Native Fi IPv6 and NAT64 are still untested.** The active profile requests
   IPv4 only. The priority rule prevents Fi DNS64 from contaminating Wi-Fi; it
   does not establish that Fi's own DNS64 answer has a working Fi NAT64 egress
   path.
3. **This is a static preference, not health-aware DNS failover.** Test an
   associated-but-unusable Wi-Fi network (bad upstream, captive portal, or dead
   resolver) before assuming resolved will move queries to Fi at the desired
   point.
4. **Git-over-HTTPS has not been validated as a Fi-only Git fallback.** The
   GitHub web HTTPS probe succeeds, but a real Git HTTPS authentication and
   transfer path is a separate test.
5. **It does not change the Cilium/Nebula cellular MTU issue** described below.

Using a public resolver is still a possible complement: a Fi-bound Cloudflare
DNS-over-HTTPS query to `1.1.1.1` returned valid NODATA for GitHub's AAAA
record rather than Fi's synthetic answer. That alone cannot repair the
Fi-bound SSH timeout, and it has not been tested as a plain-DNS or failover
policy.

### Full solution work

A complete fix should make ordinary Git operations reliable while **Fi is the
only usable uplink**, not merely avoid Fi while Wi-Fi is present. Do these
experiments before choosing a permanent transport or DNS policy:

1. Run `bash debug/rugged/fi-network-diagnose.sh` and preserve the filtered
   `tcpdump` output for both GitHub SSH endpoints. Repeated outbound SYNs with
   no SYN-ACK locate the current failure beyond rugged's TCP stack; a reply
   changes the next diagnostic layer. Do not use only `ssh -b <Fi address>`:
   it does not bind egress to Fi on this route table.
2. Run `bash debug/rugged/fi-ipv4-mtu-probe.sh` to remeasure the direct Fi IPv4
   path with the temporary runtime MTU lift. This tells us whether the 1200
   cap is still necessary for direct traffic or only conservative protection.
3. Create a temporary, reversible IPv4v6 Fi profile and test its address,
   default route, minimum 1280-byte behavior, DNS64, and NAT64 access to an
   IPv4-only destination. This determines whether native Fi IPv6 can be
   enabled safely; it must not be inferred from the IPv4 probe.
4. Test the real Git HTTPS path over Fi if an HTTPS remote is to be considered
   as a fallback. Do not claim that a successful browser-style `curl` proves
   Git authentication, credential handling, or large transfers.
5. Decide and test the failure policy for connected-but-bad Wi-Fi. Static DNS
   priority may be the right default, but it is not a substitute for an
   explicit connectivity/failover design.

After the direct Fi SSH capture, either fix a local routing/firewall issue or
escalate evidence of the carrier/upstream failure. Until then, `dns-priority`
is a targeted workaround for the mixed-link stall, not a resolution of Fi
connectivity.

To re-verify the deployed stopgap with both links connected:

```bash
nmcli -g ipv4.dns-priority connection show 'Google Fi'
resolvectl --cache=no query github.com
getent ahosts github.com
git ls-remote origin
```

There should be no Fi-synthesized `64:ff9b::/96` address in the normal GitHub
answer, and the SSH connection should begin on Wi-Fi IPv4 without waiting for
the unrelated IPv6 path.

### Native Google Fi IPv6: unresolved, not disproven

The modem's initial bearer advertises `ipv4v6`, but the active bearer is
currently `ipv4` because the NixOS profile requests that explicitly. The
historical ~1256-byte ceiling was obtained with **IPv4** DF-ping probes. It
justifies the current IPv4 MTU 1200 workaround, but does **not** prove the
native Fi IPv6 path has the same ceiling.

That distinction matters: IPv6 requires every link to carry packets of at
least 1280 bytes, or to provide fragmentation/reassembly below IPv6. If Fi's
native IPv6 path truly could not do that, it would be a carrier or tunnel
misconfiguration that should affect ordinary devices too. A normal cellular
implementation can hide a smaller radio/tunnel payload below IPv6; Windows
devices need not experience the same failure merely because this host's IPv4
DF probe found a smaller path.

The current workaround was therefore conservative, not a conclusive native
IPv6 diagnosis. Before changing it, run a controlled, reversible test with
Wi-Fi kept up:

1. Revalidate the present IPv4 DF-ping ceiling after temporarily lifting only
   the runtime `wwan0` MTU, then restore 1200.
2. Activate a temporary IPv4v6 Fi profile and record its IPv6 address, default
   route, DNS64/NAT64 route, and 1280-byte packet behavior.
3. Restore the declared IPv4-only profile regardless of result.

Only that test can distinguish a current Fi/carrier defect from an obsolete or
overbroad local workaround. Do not claim that native Fi IPv6 is broken until
then.

### Nebula and Cilium: a separate, real cellular MTU problem

Rugged is a Kubernetes worker. Its cluster traffic is not direct Internet
traffic: pod packets use Cilium VXLAN, then the Nebula `nebula1` TUN, before
the encrypted Nebula UDP packet uses the current physical underlay. The live
routes make the boundary explicit:

| Path                       | Live route/MTU          | Relevance                     |
| -------------------------- | ----------------------- | ----------------------------- |
| Public Internet            | Wi-Fi/Fi default routes | Direct GitHub and DNS traffic |
| Nebula mesh `10.42.0.0/16` | `nebula1`, MTU 1420     | Cluster node traffic          |
| Pod CIDRs `10.244.0.0/16`  | Cilium routes, MTU 1370 | Pod and ClusterIP traffic     |

There is no overlay default route, and an explicitly `wwan0`-bound probe does
not traverse Nebula or Cilium. Thus the overlay cannot explain the direct
GitHub DNS64 selection or its Fi-bound SSH timeout.

It **can** explain large-packet failures to the cluster on cellular. The
confirmed incident in
<../../cluster/debug/2026-06-02-tofu-apply-hangs-from-rugged-mtu.md> showed
large Cilium-over-Nebula packets silently dropping on the Fi underlay and
causing Terraform state-write timeouts. The cluster's authoritative MTU model
is <../../cluster/docs/network.md>. Treat that as a separate roaming-node
problem: validate a host-specific MSS/PMTU mitigation before changing the
global Cilium MTU, because a global reduction penalizes every fixed-underlay
node and still does not prove anything about native Fi IPv6.

Nebula's DNS configuration is also intentionally split: only
`*.nebula.allegedly.works` queries go to lighthouse DNS; it has no role in
resolving public names such as `github.com`.

### Related, but separate: cellular MTU

The profile currently applies an IPv4 MTU of 1200 after historical PMTU/PMTUD
reports. The preserved decisive failure involved Cilium-over-Nebula traffic,
so direct Fi IPv4 still needs the dedicated probe above. Either result is
separate from the DNS64/Wi-Fi IPv6 path failure.
