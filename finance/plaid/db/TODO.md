# plaid TODO

- [x] Plaid transaction sync now uses one persisted `/transactions/sync` cursor
      per Item, applies complete added/modified/removed deltas atomically, and
      restarts pagination from the saved cursor after
      `TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION`. Signed webhook events queue
      incremental syncs; the daily CronJob catches up missed notifications.
