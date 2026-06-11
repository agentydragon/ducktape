# OVH Hillsboro availability queries

Use this when checking sub-$100/month OVH dedicated-server stock in Hillsboro
(`hil`) before ordering replacement Talos nodes.

These queries use OVH's public API and do not need OVH credentials. It is still
fine to run them from `cluster/` so the same shell is ready for the follow-up
Talos and Terraform checks. Do not print the full direnv environment while doing
this; `cluster/.envrc` contains live credentials.

## Quick single-plan check

This is the fastest way to answer "is KS-5 orderable in HIL right now?":

```bash
curl -fsS \
  'https://api.us.ovhcloud.com/1.0/dedicated/server/datacenter/availabilities?datacenters=hil&planCode=24sk502-us'
```

The response has one row per sellable configuration, usually a memory/storage
combination. For example, KS-5 can return four rows: 32/64 GiB times HDD/NVMe.
Those rows are not server counts.

Useful availability values:

- `1H-high`, `1H-low`: orderable with one-hour delivery class.
- `72H`: orderable with slower delivery.
- `comingSoon`: visible in the catalog but not orderable yet.
- `unavailable`: not orderable for that configuration.

## Sub-$100 HIL table

Run the helper script from the repo or from `cluster/`. It queries both the Eco
catalog (Kimsufi/SYS/RISE) and the regular bare-metal catalog, joins in CPU and
default memory/storage metadata, and aggregates availability by plan code.

```bash
python3 cluster/scripts/ovh_hil_availability.py
```

The script defaults to `--datacenter=hil`, `--ovh-subsidiary=US`, and
`--max-monthly-usd=100`. Use `--help` for the small set of override flags.

## Hetzner price comparison

The cluster no longer provisions HCloud nodes, but the old server-type helper is
kept for occasional bang/buck comparisons against OVH:

```bash
python3 cluster/scripts/hcloud_server_types.py --location hil
```

This uses the `hcloud` CLI from the repo devShell. If the CLI requires a token,
set it explicitly for the command; `cluster/.envrc` intentionally does not export
the legacy HCloud token.

## Interpreting the output

Treat the table as an ordering shortlist, not an inventory ledger. OVH's
availability endpoint exposes whether each memory/storage configuration is
orderable in a datacenter; it does not expose a reliable count of physical
servers available to buy.

For Talos control-plane replacement in this cluster:

- Prefer two identical plan codes so the Terraform slots have uniform hardware.
- Prefer `1H-*` or `72H` rows; `comingSoon` and `unavailable` are not actionable.
- Favor ECC RAM and non-game server lines for control-plane nodes when prices
  are close.
- After ordering, continue with the service-name/Talos flow in
  `cluster/docs/kimsufi_provisioning.md`.

## Example snapshot

Captured on 2026-05-27 19:30 PDT. Re-run the command before ordering; OVH stock
changes frequently.

- `24sk502-us` (`KS-5 | Intel Xeon-E3 1270 v6`, $20/month) was
  `comingSoon:4`, so it was visible but not orderable.
- Practical immediate candidates included:
  - `24sys032-us` (`SYS-3 | Intel Xeon-E 2288G`, $60/month):
    `1H-high:2, 1H-low:4, unavailable:6`.
  - `24rise01-v1-us` (`RISE-1 | Intel Xeon-E 2386G`, $70/month):
    `1H-high:8, 1H-low:7, 72H:18`.
  - `24rise02-v1-us` (`RISE-2 | Intel Xeon-E 2388G`, $80/month):
    `1H-high:2, 1H-low:9, 72H:22`.
  - `21adv01-v1-us` (`Advance-1 Gen 2 | Intel Xeon-E 2386G`, $98/month):
    `1H-high:8, 1H-low:8, 72H:23`.
- Lower-priced game-line options also appeared (`KS-GAME`, `SYS-GAME-1`,
  `BFGAME-1`), but they should be a deliberate choice rather than the default
  for control-plane capacity.

## References

- OVH order guide:
  <https://support.us.ovhcloud.com/hc/en-us/articles/360002250624-How-to-order-a-Dedicated-Server>
- OVH public Eco catalog:
  <https://api.us.ovhcloud.com/1.0/order/catalog/public/eco?ovhSubsidiary=US>
- OVH public bare-metal catalog:
  <https://api.us.ovhcloud.com/1.0/order/catalog/public/baremetalServers?ovhSubsidiary=US>
- OVH HIL availability endpoint:
  <https://api.us.ovhcloud.com/1.0/dedicated/server/datacenter/availabilities?datacenters=hil>
