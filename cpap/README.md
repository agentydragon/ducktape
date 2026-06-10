# CPAP

Nightly sync of ResMed CPAP data from the ez Share WiFi SD card into the
private Forgejo repo `cpap-data/cpap-data`, plus the `cpap` analysis skill
(`skill/`).

## Status

Suspended: the CronJob is pinned to wyrm2, which is offline during relocation
(see `cluster/docs/plan.md`). On the first run after unsuspending, the fresh
repo is re-seeded with the card's full history (~17+ min over the card's WiFi;
the old PVC-era data was discarded).

## Background

- ez Share WiFi SD card in ResMed AirSense 11 at home
- Card AP: SSID `Rai CPAP ez Share`, IP `192.168.4.1`
- Card firmware: `LZ1801EDPG:1.0.0`, XML API at `/client?command=...` + `/download?file=` (8.3 short filenames)
- Data: `STR.EDF` (daily summary) + `DATALOG/<date>/*.edf` (~2.5 MB/night)
- WiFi stick: `wlx9cefd5f62ee0` (MediaTek MT7921, 2.4 GHz), passed through to wyrm2 VM
- Card WiFi credentials: SOPS at `secrets/shared/cpap-ezshare.yaml`

## Architecture

- `card.py` — stdlib ez Share HTTP client: XML file listing (`GETFILELIST`),
  recursive walk, downloads. Uses IP `192.168.4.1` directly (container doesn't
  inherit host's systemd-resolved routing domain).
- `gitstore.py` — partial-clone git plumbing (subprocess git CLI):
  `clone --depth=1 --filter=blob:none --no-checkout` + `read-tree HEAD`, lazy
  blob reads, stage/commit/push. Why not pygit2 like augur's evidence scraper:
  the archive is ~1 GB/yr of binary EDFs and libgit2 has no partial clone, so a
  full shallow clone would re-transfer everything nightly. The partial clone
  keeps the nightly transfer at KBs + the new files.
- `sync.py` — the policy: clone, read the committed `sync_meta.json` manifest
  (path → size + card timestamp), `nmcli connection up cpap-ezshare`, download
  card entries that don't match their manifest entry, disconnect, commit + push
  if anything changed. The manifest replaces the PVC-era stat check (git
  discards mtimes); recording the _stored_ byte count makes mid-write downloads
  self-heal on the next run.
- Image: `ghcr.io/agentydragon/cpap-sync` (`//cpap:image`,
  `debian:bookworm-slim` + `network-manager` + `git` via apt manifest), built
  by `push-images.yml`, tagged `devel-*` for Flux image automation.
- Cluster: `cluster/k8s/cpap-sync/` — CronJob (hostNetwork on wyrm2,
  `dnsPolicy: ClusterFirstWithHostNet` to resolve Forgejo) + namespace
  (`pod-security.kubernetes.io/enforce: privileged` — needs NET_ADMIN,
  hostNetwork, hostPath).

## Credentials

`tf/gitops/cpap-data/` provisions the Forgejo repo, a `cpap-data` writer user,
a `cpap-data-reader` read-only collaborator, and two Secrets in the
`cpap-sync` namespace:

- `cpap-data-git-write` — used by the CronJob (`GIT_USERNAME`/`GIT_PASSWORD` +
  `repo_url`).
- `cpap-data-git-read` — for analysis (Claude Code); reflected into
  `claude-sandbox`. See `skill/SKILL.md` for clone recipes.

## Manual run (laptop near the CPAP)

```bash
kubectl -n cpap-sync get secret cpap-data-git-write -o jsonpath='{.data.username}' | base64 -d  # etc.
GIT_USERNAME=cpap-data GIT_PASSWORD=... bazelisk run //cpap:sync -- \
  --git-url https://git.allegedly.works/cpap-data/cpap-data.git \
  --nm-connection ''   # '' = already on the card's WiFi; otherwise a NM profile name
```

Useful if the in-cluster initial seed push ever fails: any machine on the
card's WiFi with write creds can do the re-seed.

## Future

- **USB stick placement (declarative)**: the WiFi stick is manually plugged
  into wyrm2; ideally declared in `terraform/main/proxmox-nodes.tf` via USB
  passthrough.
- **Roaming devices**: with the PVC gone, the sync can run from any machine
  near the CPAP (see `cluster/docs/plan.md`).
- **Listing efficiency**: the card's `GETFILELIST` walk enumerates every
  directory each run; if listing ever gets slow, the `date` field in
  `FileEntry` could skip old `DATALOG` directories entirely.
