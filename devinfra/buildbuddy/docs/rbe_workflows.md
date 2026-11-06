# Why no workflow needs `--remote_executor=""`

Keep RBE enabled. Module extensions, repository rules, and completed `bazel run`
programs execute on the Bazel client; build actions stay remote, except explicitly
local non-Docker live tests. See the [execution policy](../../docs/bazel_configuration.md#policy).

For regeneration and output-download/copy steps, use [lockfile workflows](../../docs/lockfiles.md)
and [Syrupy snapshot updates](../../docs/syrupy_snapshots.md#updating-snapshots).
