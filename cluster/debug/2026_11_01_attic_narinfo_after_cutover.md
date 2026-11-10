# Attic NAR hash mismatch after the local-storage cutover

On 2026-11-02 UTC, a refill of the new Attic `main` cache on wyrm2 failed to
substitute `/nix/store/c0cz14i0llk3jss3slcag1jqh5131jd2-openvino-2026.1.2`.
The `pkg-openvino-npu` and dependent `pkg-llama-cpp-openvino` targets both
reported the same error: Nix expected NAR SHA-256
`05e3b58c990203048d71c091c0093baf61f7242350be3fc4ec8d5773ba4e8399`
but received
`f331353a1a75a792b0fb402e8a9fd530b2daeca4e2d5cbe4e8e00e3fb3c0c7bd`.
The refill was paused while the object was checked.

A direct authenticated read of the current `main` narinfo returned the latter
hash. A separate download and decompression of its NAR also produced that hash,
so the served NAR agreed with the current server metadata. A fresh Nix build of
`pkg-openvino-npu` with both `narinfo-cache-positive-ttl` and
`narinfo-cache-negative-ttl` set to `0` then substituted successfully. The
remaining refill completed with those options. Both caches' NAR contents and the
whole cache were not audited; the object check applies only to this path.

The evidence points to stale Nix binary-cache metadata: the cache endpoint URL
was reused when Attic moved to a fresh database and storage, and wyrm2's positive
narinfo TTL is 2,592,000 seconds (30 days). We did not capture the old metadata
record before the refresh, so its origin and exact age are not established. The
OpenVINO derivation's output is input-addressed and does not pin a fixed output
hash, so the same store path can have different content if builds are not
reproducible.

For a recurrence, compare the current authenticated narinfo's `NarHash` with a
hash of the served, decompressed NAR before concluding the server is corrupt.
If they agree but the client expects a different hash, retry that command with
`--option narinfo-cache-positive-ttl 0 --option narinfo-cache-negative-ttl 0`.
This refreshes metadata for that command without deleting the user's local Nix
cache or changing global configuration. Other clients with cached metadata may
encounter the same symptom; refreshing one machine does not refresh them.

The separate GitHub-hosted refill failure was disk exhaustion, tracked in
[#13704](https://github.com/agentydragon/ducktape/issues/13704).
