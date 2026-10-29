# CPAP

Nightly sync of ResMed CPAP data from the ez Share WiFi SD card into the
private Forgejo repo `cpap-data/cpap-data`, plus the `cpap` analysis skill
(`skill/`).

## Status

The CronJob runs on the always-on `optiplex` Talos worker. KubeVirt runs a small
gateway VM there too, with the USB WiFi stick passed through from the host. The
VM associates with the card AP and exposes a narrow HTTP bridge through the
internal `cpap-card` Service; the sync job no longer needs host networking or
WiFi capabilities.

The gateway logs its kernel and journal to the serial console, which KubeVirt's
`guest-console-log` container ships to Loki. A minute timer runs `cpap-gateway-health`
(<gateway/health.py>, released as a wheel and pinned into the guest image). It keeps
the card's NetworkManager profile (in memory, from the KubeVirt secret disk) and logs
each change between `connected`, `card-away` (the radio sees other networks but not
the card, which is normal while the CPAP travels), `card-visible`, and the two
wedged-adapter states `no-wifi-device` and `radio-deaf` (a scan finds no networks at
all). The card bridge listens only while `connected`, so a sync against an absent
card is refused at once. In a wedged state the timer alternately reloads `mt7921u`
and re-authorizes the USB adapter every five minutes.

If the card's known WiFi MAC appears as the factory-default `ez Share` network,
the health check reports `card-reset` and temporarily associates with that BSSID.
It verifies the card's HTTP-reported identity before restoring the configured
SSID and the WiFi/administrator passwords from the secret disk, preserves the
card's channel, and reconnects normally. The bridge stays stopped during recovery;
failed attempts retry every five checks. A different card advertising `ez Share`
does not trigger recovery. MAC checks prevent accidental recovery of another
card; they cannot authenticate hardware against deliberate MAC spoofing.

On the first run after this migration, the fresh repo is re-seeded with the card's
full history (~17+ min over the card's WiFi; the old PVC-era data was discarded).

## Background

- ez Share WiFi SD card in ResMed AirSense 11 at home
- Card AP: SSID `Rai CPAP ez Share`, IP `192.168.4.1`
- Card firmware: `LZ1801EDPG:1.0.0`, XML API at `/client?command=...` + `/download?file=` (8.3 short filenames)
- Data: `STR.EDF` (daily summary) + `DATALOG/<date>/*.edf` (~2.5 MB/night)
- WiFi stick: MediaTek MT7921U passed through to the KubeVirt gateway VM on OptiPlex (2.4 GHz USB adapter)
- Card WiFi credentials: SOPS at `secrets/shared/cpap-ezshare.yaml`

## Architecture

- `card.py` — stdlib ez Share HTTP client: XML file listing (`GETFILELIST`),
  recursive walk, downloads. The cluster uses the `cpap-card` Service; the
  KubeVirt gateway forwards it to `192.168.4.1` over the card WiFi.
- `gitstore.py` — partial-clone git plumbing (subprocess git CLI):
  `clone --depth=1 --filter=blob:none --no-checkout` + `read-tree HEAD`, lazy
  blob reads, stage/commit/push. Why not pygit2 like augur's evidence scraper:
  the archive is ~1 GB/yr of binary EDFs and libgit2 has no partial clone, so a
  full shallow clone would re-transfer everything nightly. The partial clone
  keeps the nightly transfer at KBs + the new files.
- `sync.py` — the policy: clone, read the committed `sync_meta.json` manifest
  (path → size + card timestamp), download card entries that don't match their
  manifest entry, then commit + push if anything changed. Direct WiFi
  association remains available for manual laptop runs; the cluster passes an
  empty `--wifi-interface` and uses the in-cluster gateway VM. The manifest replaces the
  PVC-era stat check (git discards mtimes); recording the _stored_ byte count
  makes mid-write downloads self-heal on the next run.
- Image: `git.allegedly.works/ducktape-ci/cpap-sync` (`//cpap:image`,
  `debian:trixie-slim` + `wpa_supplicant` + `iw` + `iproute2` + `dhclient` + `git` + `ca-certificates` via
  apt manifest), built by `push-images.yml`, tagged `devel-*` for Flux image
  automation.
- Cluster: `cluster/k8s/cpap-sync/` — CronJob and KubeVirt gateway VM on
  `optiplex`, a selector-backed `cpap-card` Service, and a Cilium egress policy
  that grants the sync workload only DNS, the card bridge, and Forgejo's
  cluster-facing ports. The WiFi credential is consumed only by the gateway VM's
  NetworkManager bootstrap service.

## Credentials

`tf/gitops/cpap-data/` provisions the Forgejo repo, a `cpap-data` writer user,
a `cpap-data-reader` read-only collaborator, and two Secrets in the `forgejo`
namespace, which ESO copies into `cpap-sync`:

- `cpap-data-git-write` — used by the CronJob (`GIT_USERNAME`/`GIT_PASSWORD` +
  `repo_url`).
- `cpap-data-git-read` — for analysis (Claude Code); reflected into
  `claude-sandbox`. See `skill/SKILL.md` for clone recipes.

## Manual run (laptop near the CPAP)

```bash
kubectl -n cpap-sync get secret cpap-data-git-write -o jsonpath='{.data.username}' | base64 -d  # etc.
GIT_USERNAME=cpap-data GIT_PASSWORD=... bazelisk run //cpap:sync_bin -- \
  --git-url https://git.allegedly.works/cpap-data/cpap-data.git \
  --wifi-interface ''   # '' = already on the card's WiFi; otherwise CPAP_WIFI_PASSWORD is required
```

Useful if the in-cluster initial seed push ever fails: any machine on the
card's WiFi with write creds can do the re-seed.

## Future

- **USB stick placement**: the WiFi stick is physically attached to OptiPlex;
  Talos exposes it to KubeVirt and the gateway VM owns the userspace
  association. Restart the VM after rotating `cpap-ezshare`, since the secret
  disk is consumed during boot.
- **Roaming devices**: with the PVC gone, the sync can run from any machine
  near the CPAP (see `cluster/docs/plan.md`).
- **Listing efficiency**: the card's `GETFILELIST` walk enumerates every
  directory each run; if listing ever gets slow, the `date` field in
  `FileEntry` could skip old `DATALOG` directories entirely.
