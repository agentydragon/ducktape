# Execution input tree listing

`bbapi execution files` calls BuildBuddy's `GetExecutionDownloads` RPC and emits its paginated
file records. It does not fetch the remote Action or traverse that Action's `input_root_digest` as a
REAPI `Directory` tree. Its result is not a complete inventory of action inputs.

A read-only comparison of an existing pipeline execution's returned records, Action and Command
metadata, and cached input-root Directory tree found a reviewed authored spec file in the tree that
was absent from the three returned records. The file's digest matched the reviewed content. The
private invocation, paths, digest, and content are not retained here.

The CLI has no CAS Action-blob reader or input-tree mapper. A complete input listing is a separate
feature: resolve the execution's Action digest, read the Action from CAS, take its input-root digest,
then enumerate it through BuildBuddy's `GetTree` RPC and the REAPI `Directory` messages. The feature
must resolve the right instance and digest function, finish all `GetTree` pages, and report failure
instead of presenting a partial tree as complete. Use neutral fixture paths and content.

`GetExecutionDownloads` pagination also has a 100-page safety cap. The CLI now returns an error if
another page remains at that limit, rather than emitting a truncated list as if it were complete.
