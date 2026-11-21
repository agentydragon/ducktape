# PR visuals TODO

- Add a pre-commit check that rejects newly added PNGs under `**/__screenshots__/`,
  `**/__snapshots__/`, and `**/baselines/`. Allow genuine input fixtures, including
  archived specimens under `props/specimens/**`.

- Design optional screenshot comparison tolerances for producers with measured pixel drift.
  Keep exact comparison as the default. Evaluate channel intensity, changed-pixel fraction,
  and masks. Test no-op noise and real changes across supported writers. Consider
  producer-supplied preview priority separately.
- Bound publication by artifact count, encoded bytes, decoded dimensions, and total decoded
  pixels. Set operation deadlines and retry budgets across listing, download, decoding,
  rendering, and upload. Test partial failures and older runs finishing last on the same PR
  head. Keep missing producer output, invalid artifacts, and unavailable dependencies
  distinguishable.
