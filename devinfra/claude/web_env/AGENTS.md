@README.md

- Keep tokens redacted in `reference/` snapshots.
- Bake reproducible files into the image rather than adding exclusions;
  `volatile_paths` for nondeterministic tool installs, `only_in_live` only for the
  truly unreproducible.
- Debs are fetched into gitignored `local_debs/`; when package versions change, keep
  `live-dpkg-versions.txt`, `tools/fetch_debs.py` `SNAPSHOT_DATES`, and the fetch
  verification aligned.
- Full container refresh: use the `update_container_re` skill (binary capture, RE
  review, snapshot-to-diff, docs refresh).
- READMEs describe only the current binary/container version; historical change
  notes stay with per-version artifacts or `diff_report.md`.
