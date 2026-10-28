# Holdings capture backfill preflight

Read-only live inspection through the existing plaid-pgweb connection as
`plaid_ro`, 2026-10-28 04:32–04:34 UTC. No database writes or syncs were run.
Schema revision: `0009`. Source reviewed: `fd8dc826ee`.

## Observations

| Check                                                                               | Result |
| ----------------------------------------------------------------------------------- | -----: |
| Holding rows                                                                        |  1,355 |
| Distinct `(item_id, captured_at)` holding batches                                   |    338 |
| Successful holdings API responses retained                                          |    342 |
| Failed holdings API events                                                          |    163 |
| Successful responses containing an explicit empty holdings array                    |      0 |
| Successful responses missing/null holdings or accounts arrays                       |      0 |
| Stored batches with exactly one matching response                                   |    338 |
| Unmatched or ambiguously matched stored batches                                     |      0 |
| Matching responses from succeeded sync runs                                         |    337 |
| Matching response from a later-failed sync run                                      |      1 |
| Other successful responses, whose Item no longer exists                             |      4 |
| Covered existing account observations, each with a balance object and current field |    338 |
| Account–Item ownership mismatches across normalized tables                          |      0 |
| Duplicate `(item_id, captured_at, account_id, security_id)` positions               |      0 |
| Nonfinite transaction/balance/holding/investment values                             |      0 |
| Transaction amounts differing from their stored JSON decimal representation         |      0 |

Additional table counts: 4,003 transactions, 1,189 balance snapshots, 7,089
investment transactions. The holdings history starts 2026-08-10; latest capture
was 2026-10-28 00:17 UTC.

## Matching method

Group stored holdings by Item and capture timestamp. Canonically aggregate each
group's complete raw JSON, ordered by account, security, and JSON text. Compare
with the successful `investments/holdings/get` response's holdings array,
canonically ordered the same way. Require the group's capture time to fall
between its linked sync run's start and the API event's recorded time.

Check uniqueness in both directions. All 338 retained batches currently have
exactly one matching event. The four unmatched API responses belong to removed
Items and have no retained batch; do not recreate deliberately removed accounts
or holdings from those audit records.

## Backfill shape

- Create one capture per uniquely matched stored batch, preserving its original
  captured_at and referencing the matching API event.
- Recover account coverage and balances from that event's accounts array.
- Link existing holding rows to the capture without changing their values.
- Do not require whole-run success: one batch was committed before another
  endpoint failed, and the stored rows match its received payload exactly.
- There are no historical empty responses to recover in this dataset. Exercise
  empty, missing, and failed-application cases with synthetic migration/import
  tests; the new writer must commit capture, coverage, and rows atomically.
- Add decimal columns using the retained decimal representation where available;
  conversion cannot recover upstream precision lost before JSON was stored.
- Recheck ownership and uniqueness immediately before validating constraints.

Use an additive, idempotent migration/backfill with an API-event uniqueness key.
Validate coverage, row counts, values, and totals before switching readers or
making new capture references required. Re-run this preflight against the data
present at migration time; these counts are a snapshot, not permanent assertions.
