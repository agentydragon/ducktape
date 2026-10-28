# BuildBuddy protos archive integrity drift

The BuildBuddy proto module extension fetches a GitHub-generated `.tar.gz`
archive with a pinned `integrity` hash in
[`third_party/buildbuddy/buildbuddy_protos.bzl`](../../third_party/buildbuddy/buildbuddy_protos.bzl).
GitHub can regenerate an archive for the same commit with different compressed
bytes, which makes the repository fetch fail its integrity check. An RBE build
may appear healthy if its result is already cached, so reproduce a fetch failure
locally before changing the pin.

The recorded recovery was to download the archive and update the `integrity`
value. This review confirmed that the source still uses the commit archive and
an integrity pin; it did not reproduce a new checksum mismatch. Keep this as a
historical failure mode rather than treating it as a current build failure.
