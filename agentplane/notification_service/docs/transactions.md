# Source and inbox transaction contract

This contract governs combined source and inbox writes and the matching and
projection boundaries inside the Notification Service. The service, database,
protobufs and deployed names stay as they are.

## Authority and participants

`internal/transaction.Runner` owns begin, commit and rollback for Notification
Service writes. Production wiring shares one runner between Store and GitHub state.
It delegates the SQL lifecycle to `postgres.Transaction`; neither participant owns
that lifecycle. Its callback receives a concrete `transaction.Tx` wrapping an
unexported `*sql.Tx`, with only `QueryContext`, `QueryRowContext` and `ExecContext`.
There is no unwrap, commit, rollback, begin or database accessor on this capability.
SQL argument lists use the driver's variadic SQL values; domain boundaries remain
closed typed sums.

Participant methods accept `transaction.Tx` and perform their writes directly.
An operation opens one runner callback at its outer boundary; a participant never
calls another transaction-opening method from that callback. Reads outside a write
transaction may use the database directly. Lease and notification helpers consume
only the query/exec capability they need.

`internal/sources/github/publication` owns the concrete GitHub wake publication
adapter. GitHub state calls it explicitly in its transaction; `Effects{WakeApp,
Publish}` function injection disappears. Store retains generic scheduling and append
primitives. Subsequent matching and projection extractions use this same capability
and adapter, without giving generic inbox code GitHub interpretation.

## Typed append boundary

The common `model` package retains owner, destination, Actions, Message and
provider-neutral persistence values throughout the transaction, codecs, fanout and
projection tracks. Placement under `internal/inbox` belongs to the later inbox-owner
track.
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

`EntryPayload` carries canonical JSON bytes and typed native content columns.
Source-owned codecs produce it before append; generic storage persists it without
interpreting GitHub payloads. Existing JSON bytes, protobuf bytes, native values
and nanosecond timestamp residuals retain their encodings. Read adapters reconstruct
the explicitly typed source variants from the existing columns. No key column,
SQL migration, protobuf change or new uniqueness rule is part of this extraction.
The fanout and projection tracks implement this append boundary; the transaction
track establishes its shared capability first.

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

## Validation and rollback

Keep existing golden values unchanged. Run the tracks' state integration, Store
integration, model, golden and service integration Bazel targets with `bbr`; retain
coverage for rollback after publication failure, fence expiry during writes,
multiple matches, replay after expiry and quiet refreshes.

Rollback reverts the implementation commits and rolls the previous service image.
Existing rows and both protobuf contracts remain readable without conversion.
After rollout, check readiness, a fresh Action subscription, GitHub ingestion,
inbox reads, replay dedupe and notice delivery.
