# Syrupy Snapshot Workflow

Snapshot tests use [syrupy](https://github.com/syrupy-project/syrupy) to compare
test output against stored `.ambr` files in `__snapshots__/` directories.

## Setup

Set `uses_syrupy = True` and declare each checked-in Amber snapshot in the test's `data`.

```starlark
py_test(
    name = "test_foo",
    srcs = ["test_foo.py"],
    data = ["__snapshots__/test_foo.ambr"],
    uses_syrupy = True,
    deps = [..., "@pypi//syrupy"],
)
```

## Updating snapshots

Run the complete target with `--snapshot-update` on RBE. For example, from the repository root:

```bash
bbr test //difftree:test_snapshots --test_arg=--snapshot-update --test_arg=--snapshot-details --nocache_test_results
```

Set `invocation_id` to the Bazel invocation ID printed by `bbr` and download the updated collections into the target's
snapshot directory. For the example above:

```bash
bbapi artifact download "$invocation_id" --kind test '*.ambr' --all -o difftree/__snapshots__
git diff -- difftree/__snapshots__
bbr test //difftree:test_snapshots
```

Syrupy runs its normal Amber writer and removes unused entries. The Bazel pytest plugin exports each
changed collection that still exists after cleanup. When a whole collection is removed, the test log
lists its deleted snapshots under `--snapshot-details`. Remove that file and its `data` entry from
the checkout. When creating a collection, add the downloaded file to `data` before rerunning the test.
