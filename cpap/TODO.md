# TODO

- **E2E-test the git-based sync** once wyrm2 + the CPAP are reachable again:
  unsuspend the CronJob (resume steps tracked in `cluster/docs/plan.md`), watch
  the first run re-seed the card's full history into the `cpap-data` repo
  (~17+ min), confirm the commit lands in Forgejo and that a second run is a
  fast no-op.
- **Generalize the sync beyond wyrm2** — the git-based store removed the PVC
  pinning, so any machine near the CPAP with the write creds can run it:
  - `rugged` (roaming NixOS laptop): provision the `cpap-ezshare` NetworkManager
    profile (mirror `nix/nixos/hosts/wyrm2/default.nix`) + write creds, and run
    the sync from a systemd timer — or keep the k8s CronJob and relax the
    nodeSelector to whichever host is home with the WiFi stick.
  - Possibly `pixel6`: the sync itself is just python3 + git + the card's WiFi,
    but needs a runner story on Android (Termux or similar).
