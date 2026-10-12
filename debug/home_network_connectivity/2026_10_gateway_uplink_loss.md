# Packet loss on the switch-to-gateway uplink (2026-10)

## Status

Cleared for now; we're waiting to see whether it comes back. GitHub issue #9604 holds the full
discussion. The cause is not yet known: either the 2.5G cabling is marginal, or one PHY gets
into a bad state that a renegotiation from the switch side resets.

## Symptom

Every home host (`optiplex`, `wyrm2`) lost about 22% of packets to anything past the BGW320,
and that includes the gateway's own address. The loss came as a 200-300 ms blackout every ~1 s.
Traffic that stayed on the CRS310 (host to host, host to switch) lost nothing. New TCP
connections failed because their SYN retries at +1 s and +3 s fall back into the same phase of
the cycle. That produced the Cilium Envoy 5 s connect-timeout 503s and the `mcp_unavailable`
errors on home-hosted backends.

## What was measured

- **Onset ~2026-10-05 12:00Z.** On `wyrm2`, the ratio of SYN retransmits to new connections
  stepped from ~0.02 to ~0.08-0.11 (recipe below). That is right after `optiplex` rebooted
  following its NVMe dropout (`cluster/debug/optiplex_nvme_thermal/2026_10_04_dropout.md`).
  Neither the gateway's boot (10-02) nor the switch's (10-08) lines up with it.
- **The WAN side was clean** for as long as the gateway exporter has data (from 10-10): WAN
  and PON up, steady optical levels, 0 WAN errors. The gateway's syslog holds only rate-limited
  firewall drops; per `att_gateway/README.md` the gateway keeps no link-event history.
- **The switch saw nothing locally:** no pause frames, drops or FCS errors on `ether1`. The link
  does go down and back up about 20 times a day, ~5 s each time (`mktxp_link_downs_total`,
  switch syslog). Those flaps did not clear the loss.
- **Link-speed test, 2026-10-12 20:36-20:44Z.** 10 Hz pings from `wyrm2` to `192.168.1.254`
  while changing `ether1`'s `advertise`:

  | `ether1`                  | loss to gateway       |
  | ------------------------- | --------------------- |
  | 2.5G, as found            | 22% (600 pkts)        |
  | 1G only                   | 0% (600)              |
  | 100M only                 | 0% (600)              |
  | 2.5G again, original list | 0% (1800 over ~3 min) |

  Loss to the switch was 0% throughout.

## Next

If the loss returns at 2.5G, the cabling is the likelier cause. In that case, set `ether1` to
advertise 1G in `tf/gitops/home-switch`; 1G is below the line's ~1.25 Gbps. If it stays away,
a stuck PHY is the likelier cause, and the hourly flaps are the remaining symptom to explain.

## Recipes

Mimir is `http://mimir-gateway.monitoring.svc/prometheus`. Grafana's Explore view works, as
does `curl` from a pod in a namespace whose egress reaches `monitoring`; `haku-sandbox` does.

SYN-retransmit ratio per node. This was the only history of the loss before a dedicated
prober existed. `wyrm2` is `10.42.0.20:9100` and `optiplex` is `10.42.0.18:9100`:

```promql
sum by (instance) (increase(node_netstat_TcpExt_TCPSynRetrans[1h]))
  / sum by (instance) (increase(node_netstat_Tcp_ActiveOpens[1h]))
```

Switch uplink flaps, rate and errors (mktxp):

```promql
increase(mktxp_link_downs_total{name="ether1"}[1d])
mktxp_interface_rate{name="ether1"}
increase(mktxp_switch_rx_fcs_error_total{name="ether1"}[1d])
```

Gateway side (`att_gateway_*`; WAN, optical and LAN port 1):

```promql
att_gateway_wan_up
att_gateway_optical_rx_power_dbm
increase(att_gateway_lan_port_receive_errors_total[1h])
```

Switch and gateway syslog in Loki: `{job="home-switch"}` (link up/down lines) and
`{job="home-gateway"}` (firewall drops only). These streams have no `namespace` label, so
`loki-read-proxy` can't serve them. Use Grafana. Without Grafana, query `loki-read` on port 3100
from a host-network pod; Loki's ingress policy admits the `host` entity.

Repeating the speed test: from a home host, run `ping -i 0.1 -c 600 192.168.1.254` alongside
the same against `192.168.1.100` as a control. Change the uplink with the switch REST API
(`https://192.168.1.100/rest/interface/ethernet/<id>`, `PATCH {"advertise": "1G-baseT-full"}`)
as the `tofu` user, with the password from `monitoring/home-switch-tofu-password` and the CA from
`monitoring/home-switch-tls`. First add a one-shot `/system scheduler` entry that restores the
original `advertise` list, so a link that doesn't come back reverts on its own.
