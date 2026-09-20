# TODO

- Ingest by tree diff: the store keys blobs by content hash and snapshots by path-to-blob
  manifest, so a new commit only needs the blobs `git diff` names; today every snapshot reads
  the whole tree.
