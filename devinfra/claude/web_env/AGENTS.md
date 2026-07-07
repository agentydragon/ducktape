@README.md

## Agent Instructions

- Put container filesystem content under `rootfs/`, mirroring the live paths the
  Dockerfile copies into the image.
- Put proprietary reference snapshots under `reference/`; keep tokens redacted.
- Prefer baking reproducible files into the image over adding exclusions. Use
  `volatile_paths` for nondeterministic tool installations and `only_in_live`
  only for files that truly cannot be reproduced.
- Commit `diff_report.md` with Dockerfile/rootfs changes after running the
  build-and-diff workflow from the README.
- Package fetches are generated into gitignored `local_debs/`; when package
  versions change, keep `live-dpkg-versions.txt`, `fetch_debs.py`
  `SNAPSHOT_DATE`, and the fetch verification aligned.
- For full container refreshes, use the `/update_container_re` skill; it owns
  binary capture, RE review, snapshot-to-diff, and documentation refresh.
- Parent READMEs describe only the current binary/container version. Historical
  change notes stay with the per-version artifacts or `diff_report.md`.
