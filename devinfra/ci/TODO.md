# CI TODOs

## Fetch publication artifacts explicitly from BuildBuddy

Image publication and the release-artifact action still rely on `bb remote build`'s
post-run output download. Replace that dependency with explicit BEP-based fetching
through `bbapi`, preserving the exact target identity and source invocation for
each output. Resolve OCI layouts from their target's advertised files instead of
searching the local output tree by basename. Keep registry credentials on the
GitHub runner.

The Nix artifact gate already uses the batch-fetch path. Track the remaining
publication consumers and the upstream download investigation in
<../buildbuddy/debug/bb_remote_peers_exhausted.md>.

## Scope the undeclared-outputs download narrower than "every test"

`test:rbe --remote_download_regex=...` in <devinfra/bazel/rbe.bazelrc> forces
every test's `test.outputs/` tree onto the runner, though only visual tests need
it there. The cost is small and lands only on tests that wrote undeclared
outputs (<devinfra/docs/bazel_caching.md> § Undeclared test outputs under BwoB),
but the flag is global where the requirement is not.

Uncosted: a second `bazel test` pass over the visual targets carrying the
download flags, with the main `//...` sweep left on plain minimal; or giving
those targets a different execution path entirely.
