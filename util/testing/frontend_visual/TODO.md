# TODO

- Remove `pixelmatch` + `pngjs` from `package.json` (unused since the
  compareBaseline path was deleted). Needs the managed pnpm lockfile regen
  flow (<../../../devinfra/docs/lockfiles.md>), which requires a local Bazel
  build — do it in a session with working local Bazel.
