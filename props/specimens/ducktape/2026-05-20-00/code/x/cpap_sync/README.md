# CPAP ez Share Sync

Nightly sync of ResMed CPAP data from the ez Share WiFi SD card to the cluster.

## Status

Live. CronJob running on wyrm2, syncing to `cpap-data` PVC at 10:00 UTC daily.

**Initial sync (2026-04-20)**: 6 months of data (2025-10-04 → 2026-04-18), ~700 files,
17 minutes. Subsequent runs skip already-downloaded files, so daily incremental syncs
should complete in seconds.

## Background

- ez Share WiFi SD card in ResMed CPAP at home
- Card AP: SSID `Rai CPAP ez Share`, IP `192.168.4.1`
- Card firmware: `LZ1801EDPG:1.0.0`, XML API at `/client?command=...` + `/download?file=` (8.3 short filenames)
- Data: `STR.EDF` (daily summary) + `DATALOG/<date>/*.edf` (~2.5 MB/night)
- WiFi stick: `wlx9cefd5f62ee0` (MediaTek MT7921, 2.4 GHz), passed through to wyrm2 VM
- Credentials: SOPS at `secrets/shared/cpap-ezshare.yaml`

## Architecture

- `sync.py` — stdlib-only Python: fetches full file list via `Getallfiles` XML API, skips
  existing files, downloads new ones. Uses IP `192.168.4.1` directly (container doesn't
  inherit host's systemd-resolved routing domain).
- `sync.sh` — shell script: `nmcli connection up cpap-ezshare` → `python3 /sync.py` → disconnect
- `Dockerfile` — `debian:bookworm-slim` + `network-manager python3`
- Image: `ghcr.io/agentydragon/cpap-sync`, built by `container-images.yml`, tagged `devel-*` for Flux
- Cluster: `cluster/k8s/cpap-sync/` — CronJob, PVC (`lvm-proxmox-hdd` 50Gi), SOPS secret, namespace
  - Namespace has `pod-security.kubernetes.io/enforce: privileged` (needs NET_ADMIN, hostNetwork, hostPath)

## Future

- **USB stick placement (declarative)**: The WiFi stick is currently manually plugged into
  wyrm2. Ideally declared in `terraform/main/proxmox-nodes.tf` via USB passthrough device.
- **Sync efficiency**: Initial full sync takes ~17 min. Currently skips files by checking local
  existence, so incremental daily syncs are fast. But the card has no rsync/delta protocol —
  the `Getallfiles` list still enumerates everything each run. If the card accumulates years
  of data, listing + skipping could get slow. Options to explore: filter `Getallfiles` by
  `ctime` param (unix timestamp of last sync) to only return new files; or use the `date`
  field in FileEntry to skip listing old directories entirely.
- **Data consumption**: where does the data go after sync?
  - Leave on PVC, expose via a small HTTP server or NFS for OSCAR on desktop
  - SleepHQ upload step (future)
