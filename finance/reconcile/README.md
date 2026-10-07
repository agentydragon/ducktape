# Reconciler

Helps reconciling GnuCash with external systems by matching them to transaction IDs in external systems.

Currently can match GnuCash accounts to a user's part in a Splitwise group.

The matching is done by adding `splitwise=12345` into the notes field in GnuCash, where `12345` is the Splitwise expense
ID.

```bash
bazel run //finance/reconcile | tee output.txt
```

Expects some configuration in `~/.config/ducktape/config.yaml`.
