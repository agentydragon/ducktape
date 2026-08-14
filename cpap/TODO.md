# TODO

- **Generalize the sync beyond OptiPlex** — the git-based store removed the PVC
  pinning, so any machine near the CPAP with the write creds can run it. A
  non-Talos host can use its own NetworkManager profile; a Talos host needs the
  host-networked `wpa_supplicant` path used by the CronJob.
