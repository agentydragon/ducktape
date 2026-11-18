# Source and inbox transaction contract

This contract governs combined source and inbox writes and the matching and
projection boundaries inside the Notification Service.

## Authority and participants

`internal/transaction.Runner` owns begin, commit and rollback for Notification
Service writes. Production wiring shares one runner between `internal/inbox.Store`, GitHub state
and the GitHub fanout coordinator. It delegates the SQL lifecycle to
`postgres.Transaction`; participants do not own that lifecycle. Its callback
receives a concrete `transaction.Tx` wrapping an unexported `*sql.Tx`, with only
`QueryContext`, `QueryRowContext` and `ExecContext`.
There is no unwrap, commit, rollback, begin or database accessor on this capability.
SQL argument lists use the driver's variadic SQL values; domain boundaries remain
closed typed sums.

Participant methods accept `transaction.Tx` and perform their writes directly.
An operation opens one runner callback at its outer boundary; a participant never
calls another transaction-opening method from that callback. Reads outside a write
transaction may use the database directly. Lease and notification helpers consume
only the query/exec capability they need.

`internal/sources/github/publication` owns the concrete GitHub wake publication
adapter and subscription initialization participants. It does not import `internal/inbox`.
GitHub state calls the wake adapter explicitly in its transaction.

`internal/sources/github/publication/fanout.Coordinator` owns delivery matching,
recording, ingestion, observation projection and retention. It holds the database,
shared runner and `internal/inbox.Store`. Source-owned projectors build neutral append inputs in the
same callback. `internal/inbox` provides generic claim fencing, scheduling and append
participants.

Subscription creation accepts the closed `sourceadapter.Authorization` sum:
`ActionsAuthorization` or `GitHubAuthorization`. Sourceadapter dispatches source
locking, binding and initialization explicitly. `internal/inbox` owns inbox creation,
idempotency replay, quotas and the subscription insert. Replay returns before
source grants or snapshots are written; new GitHub subscriptions establish their
grant before the subscription insert. Binding values use the existing relational
columns in `model.SubscriptionBinding`.

## Typed append boundary

The common `model` package retains owner, destination, Actions, Message and
provider-neutral persistence values used throughout the transaction, codecs, fanout,
projection and `internal/inbox`. Inbox lifecycle and persistence types belong to
`internal/inbox`; shared values stay at the common owner.
GitHub-only values and their codecs belong in
`internal/sources/github/{model,storagecodec}`. Mixed RPC/subscription sums belong
at an explicit typed adapter boundary that can import both domains. Neither domain
imports that adapter. Providers remain explicitly wired.

The neutral append input is `model.AppendInput{Key EntryKey, Payload EntryPayload}`;
inbox and subscription UUIDs are separate arguments. `EntryKey` is a relational
key, not a hash or new serialized identifier: a provider discriminator plus an
ordered tuple of existing identity columns and typed scalar values. The column
vocabulary is closed to the existing schema. The scalar sum is closed to UUID,
integer, text and SQL NULL; NULL and empty text are distinct. Source-owned adapters
construct the tuple, including all existing natural-key fields. Generic storage
compares and persists the tuple without switching on a GitHub event or provider.

`EntryPayload` carries canonical JSON bytes, typed native content columns and
message sender identity. Source-owned codecs produce it before
`inbox.Store.AppendTx(ctx, tx, inboxID, subscriptionID, input)`; generic storage persists
it without interpreting GitHub payloads. Existing JSON bytes, protobuf bytes, native
values and nanosecond timestamp residuals retain their encodings. Read adapters reconstruct
the explicitly typed source variants from the existing columns.

Common storage codecs scan the existing physical identity columns. GitHub codecs
reconstruct GitHub identities and interpret GitHub summary fields; the typed
source adapter dispatches Actions, GitHub and Message variants explicitly. GitHub
state owns subscription status queries. Due state and status cross the inbox boundary
as closed source-adapter sums, so generic storage does not interpret
GitHub observations, access bindings or payloads.

An entry key identifies one logical event within an inbox, independently of the
matching subscription. A second match adds only its subscription association.
Payload expiry clears content while preserving every identity column, so replay
still finds the original cursor.

## Atomicity, ordering and wakeups

Every combined GitHub fact/inbox write takes the GitHub ingress advisory lock
first, then inbox rows in stable UUID order. GitHub network calls finish before
entering the callback. Source claims, subscription versions, access and generation
fences, append, scheduling and wake publication share its transaction. Recheck
fences after writes, before returning success; expiry rolls back the whole page,
including facts, entries, schedule changes and transactional notifications.

A successful refresh settles `next_attempt` to SQL NULL. Changed facts publish
wakeups before commit. Quiet workers wait on committed notifications or a real due
deadline; retry timers cover storage failures only.
