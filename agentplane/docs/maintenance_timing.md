# Online maintenance timing

These controls belong to the manual maintenance Jobs. Pass them explicitly when preparing a Job:

| Command         | Flag                   | Default | What it bounds                                           |
| --------------- | ---------------------- | ------- | -------------------------------------------------------- |
| compact-journal | --lock-wait            | 5s      | Waiting for each PostgreSQL lock acquisition             |
| compact-journal | --lock-hold            | 250ms   | Time after acquiring the Session row before cancellation |
| compact-history | --lock-wait            | 250ms   | Waiting for each PostgreSQL lock acquisition             |
| compact-history | --guard-check-interval | 5s      | Delay between guard checks while paused                  |
| settle-history  | --lock-wait            | 1s      | Waiting for each PostgreSQL lock acquisition on apply    |

The lock settings accept whole milliseconds from 1ms to 1m. An outer deadline can fire sooner.
For example, History repacking reserves 10% of its batch transaction budget for cancellation;
raising its lock wait alone cannot extend that budget. The hold timer requests cancellation;
it is not a guarantee that rollback has finished by that instant.

## Staffed rollout

1. Pin a maintenance image that recognizes the flags. Keep the current Job and its arguments
   as the rollback reference. Previous sampler images used a 100ms acquisition timeout; the new
   image can reproduce that with `--lock-wait=100ms`.
2. Preview one Session with `--dry-run`, then run the same scope with `--apply` while staffed.
   Preview does not exercise write-lock contention. Compare append latency, lock waits and hold
   times, replay results, replica lag and available disk space before expanding the scope.
3. If progress stalls or append latency regresses, pause the repack between batches and review
   the metrics. Restart with the previous budgets, or remove the new flags if reverting the image.

The flags are optional for existing callers. Before making any of them required, inventory
all Job generators and operator scripts, set each value explicitly, and verify the updated
commands during a staffed window. Change omission to an error in a later release.
