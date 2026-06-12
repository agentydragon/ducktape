# Syrupy Snapshot Workflow

Snapshot tests use [syrupy](https://github.com/syrupy-project/syrupy) to compare
test output against stored `.ambr` files in `__snapshots__/` directories.

## Setup

Set `uses_syrupy = True` on the `py_test` target. This wires `BazelAmberExtension`
(<util/testing/bazel_snapshot_extension.py>) which copies updated `.ambr` files to
Bazel's undeclared test outputs for RBE retrieval, and registers a pytest plugin
that prints the update command on snapshot mismatch.

```starlark
py_test(
    name = "test_foo",
    srcs = ["test_foo.py"],
    data = ["__snapshots__/test_foo.ambr"],
    uses_syrupy = True,
    deps = [..., "@pypi//syrupy"],
)
```

The `.ambr` files must be in the test target's `data` attribute.

## Updating snapshots

### RBE (preferred — works for Docker tests)

```bash
# 1. Run with --snapshot-update and toplevel download
#    Flag order matters: --remote_download_outputs=toplevel must come AFTER
#    --config=rbe to override --remote_download_minimal.
bb test --config=rbe --remote_download_outputs=toplevel \
  //path/to:snapshot_test \
  --test_arg=--snapshot-update --nocache_test_results

# 2. Copy from undeclared outputs back to source tree
cp bazel-testlogs/path/to/snapshot_test/test.outputs/snapshot_test.ambr \
   path/to/__snapshots__/snapshot_test.ambr
```

**Why not `bbr`?** It appends `--config=rbe` after user args, so
`--remote_download_outputs=toplevel` gets overridden by `--remote_download_minimal`.
Use `bb test --config=rbe` directly for flag ordering control.

### Local (simpler, no copy step)

Local execution creates runfiles as symlinks into the source tree. Syrupy
writes through the symlink directly — no copy step needed. Use `bb test`
(the binary runs locally, build actions use RBE):

```bash
bb test //path/to:snapshot_test \
  --test_arg=--snapshot-update --nocache_test_results
```

## How it works

### Extension: `BazelAmberExtension`

Subclasses `AmberSnapshotExtension`, overrides `write_snapshot_collection()`
(the intended extension point per syrupy's class hierarchy). After the parent
writes the `.ambr` file, copies it to `$TEST_UNDECLARED_OUTPUTS_DIR` (set by
Bazel in the test sandbox). Bazel downloads undeclared test outputs to
`bazel-testlogs/<target>/test.outputs/` even with `--remote_download_minimal`
(when using `--remote_download_outputs=toplevel`).

### Wiring via `py_test` macro

`uses_syrupy = True` injects two args:

- `--snapshot-default-extension=util.testing.bazel_snapshot_extension.BazelAmberExtension`
  — syrupy uses this class for all snapshot I/O
- `-p util.testing.bazel_snapshot_extension` — registers the module as a
  pytest plugin (enables the `pytest_terminal_summary` mismatch hint)

And adds `//util/testing:bazel_snapshot_extension` to deps.

Gotcha when extending: syrupy stores the extension **class** (not an instance) in
`_queued_snapshot_writes` and calls classmethod `write_snapshot(...)` at flush;
`write_snapshot_collection` is the abstract override point. See syrupy source for
the full extension hierarchy.
