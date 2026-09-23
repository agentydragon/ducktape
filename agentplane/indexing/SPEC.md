# Git index service

One service indexes the tip of one branch of one Git remote and serves semantic search over
its regular UTF-8 text files. It is independent of the Agentplane app and its database. There
is no index identifier or collection selector.

The service reads the tree of the branch tip from its own clone and accepts only a complete
snapshot of it. A snapshot that exceeds the configured limits, or a remote that cannot be
fetched, leaves the accepted snapshot unchanged. The tree is the source of truth: Git
history is not indexed, and paths matching the configured ignore patterns, symlinks, and
submodules are not part of any snapshot.

Files advance independently. A changed file's previous version remains searchable until all
chunks of its replacement have embeddings. New files appear when ready. Deleted files disappear
when a complete replacement manifest is accepted. Only files whose contents decode as strict
UTF-8 are eligible for embedding; invalid bytes are never replaced or ignored to recover text.
NUL-containing text is also excluded. Empty and excluded files have no search hits, including
when an excluded file replaces a previously searchable version.
Identical embedding inputs reuse vectors within this service.

Search can contain multiple source revisions during an update. Every hit identifies its
repository, revision, tree id, path, and UTF-8 byte range. Search responses warn about
updates in progress or maintenance failures; status exposes desired and completed revisions
and pending file counts. An embedding failure preserves committed file versions and batches.

Restarting resumes accepted work. A superseding snapshot replaces pending membership, and
obsolete work cannot publish over it. Garbage collection retains desired, completed, and
served snapshots and collects unreachable data only after a grace period.

Search and status require the deployment's bearer credential. Health checks disclose no
indexed content. The embedding and chunking configuration is fixed for a database; changing
it requires a separate index database in this version.
