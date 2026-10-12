# Home LAN

`192.168.1.0/24` behind the AT&T BGW320 gateway. Addresses that something depends on, and who owns
each; read from the gateway's Subnets & DHCP and IP Allocation pages on 2026-10-10. The cluster
reads these fixed addresses from `cluster/cdk8s/home_lan.py`; `home-nodes.tf` and
`tf/gitops/home-switch/bootstrap.sh` repeat theirs with a sync comment.

| Address                        | What                                     | Owner                                                                                       |
| ------------------------------ | ---------------------------------------- | ------------------------------------------------------------------------------------------- |
| `192.168.1.254`                | AT&T BGW320 gateway, web UI              | Gateway default                                                                             |
| `192.168.1.64`-`192.168.1.253` | Gateway DHCP pool, 1-day leases          | Gateway Subnets & DHCP page (not in the repo)                                               |
| `192.168.1.10`                 | `optiplex`, fixed alias beside its lease | `lan_address` in `cluster/terraform/main/home-nodes.tf`                                     |
| `192.168.1.100`                | MikroTik CRS310 switch                   | Gateway IP Allocation "Fixed Allocation" (not in the repo); `tf/gitops/home-switch` uses it |

Addresses outside the pool and not listed here are free for fixed assignments. DHCP-leased hosts
(`atlas`, `wyrm2`, `optiplex`'s own lease) are not listed: their addresses can change.

## Physical wiring

```text
AT&T fiber
  -> BGW320 gateway
  -> patch panel
  -> in-wall cable
  -> living-room wall jack
  -> patch cord
  -> CRS310 switch ether1 (negotiates 2.5G)
  -> atlas (wyrm2 bridged on it as a Proxmox VM), optiplex
```

Everything home-hosted reaches the gateway, and so the internet, over the single gateway-to-`ether1`
run: the panel and the in-wall cable are part of that 2.5G link.

The switch's syslog goes to `192.168.1.10:514/udp` (`alloy-syslog`, `cluster/cdk8s/monitoring/alloy.py`).

TODO: give `atlas` (DHCP `.71` today) and `wyrm2` fixed addresses too, declared in their host
configs and listed here.
