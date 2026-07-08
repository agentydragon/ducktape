# CPAP

The operator's **ResMed AirSense 11** sleep therapy data: nightly AHI (apnea-hypopnea
index), leak rate, mask-on duration, and pressure — a direct signal on sleep quality and
therapy compliance that nothing else here surfaces. Synced nightly from the machine's ez
Share WiFi SD card into the private Forgejo repo `cpap-data/cpap-data` by the `cpap-sync`
CronJob (currently suspended during a hardware relocation — see `cluster/docs/plan.md`;
a suspended sync means recent nights may be missing, not that access is broken).

## Reaching it

Read-only, no new credential: the `haku` Forgejo account is already a collaborator on
`cpap-data/cpap-data` (repository access is scoped by Forgejo collaborators, not by the
token's own privileges — see _Setup: discover credentials_), and your existing
`haku-forgejo-tea` login already carries it. Same shape as the `haku-state` clone, over
the same **public** `git.allegedly.works` host (your home can't resolve the cluster-
internal `forgejo-http.forgejo`):

```bash
TOKEN=$(kubectl get secret haku-forgejo-tea -o jsonpath='{.data.token}' | base64 -d)

# The archive is multi-GB (EDF waveforms); partial-clone + checkout just what you need.
git clone --filter=blob:none --no-checkout "https://haku:${TOKEN}@git.allegedly.works/cpap-data/cpap-data.git" /tmp/cpap-data
git -C /tmp/cpap-data checkout main -- STR.EDF   # daily summary — start here
```

`STR.EDF` is the daily-summary EDF (one record per night): `AHI`, `Leak.50`/`.95`,
`Duration` (therapy minutes), `MaskOn`/`MaskOff`. Per-night waveforms live under
`DATALOG/<YYYYMMDD>/*.edf` (checkout the same way, one directory at a time — each night is
~2.5 MB). EDF format details, signal reference, clinical thresholds, and parsing code
(stdlib for `STR.EDF`, `pyedflib` for waveforms) are in `cpap/skill/SKILL.md` — read it
once you have files checked out rather than re-deriving the format here.
