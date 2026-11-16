# Wyrm2 node-exporter scrape from local Alloy

On 2026-11-16, `alloy-node-snxqj` on Wyrm2 (`10.244.5.12`) could not scrape
`prometheus-node-exporter-2vdks` at `10.42.0.20:9100`. The exporter Pod is
running with `hostNetwork: true` and both its Pod and host IP are `10.42.0.20`.
`curl http://10.42.0.20:9100/metrics` on the host returned HTTP 200, so the
exporter was serving metrics.

The local pod-to-host path reaches the NixOS INPUT firewall through
`cilium_net`. A passive `tcpdump -i cilium_net` captured SYNs from
`10.244.5.12` to `10.42.0.20:9100` at 11:15:28–34 UTC, including retransmits,
without a reply. The `nixos-fw` INPUT chain accepts `nebula1`, `cilium_host`,
`lxc+`, established connections, and TCP 10250, but has no rule for
`cilium_net` or TCP 9100. The preceding `KUBE-FIREWALL` chain only drops
incoming loopback-destination traffic; `CILIUM_INPUT` only accepts marked
proxy traffic. All other new SYNs reach the terminal `nixos-fw-refuse` DROP.
Its counter rose from 259591 to 259596 while four scrape SYNs were captured.
The counter includes other traffic, so that rise is corroborating evidence,
not a one-to-one packet attribution.

The declarative fix permits TCP 9100 only on `cilium_net` for NixOS Kubernetes
workers. After the host configuration is deployed, verify that the local Alloy
target reports `up=1` and Wyrm2's node-exporter series resume in Mimir, then
remove this resolved investigation note.
