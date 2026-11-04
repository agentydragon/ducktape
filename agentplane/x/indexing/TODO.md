# TODO

- Ingest by tree diff: the store keys blobs by content hash and snapshots by path-to-blob
  manifest. Today every snapshot reads the whole tree. Explore reading changed blobs with a tree
  diff while maintaining a complete, trustworthy path manifest, including deletions and renames.
