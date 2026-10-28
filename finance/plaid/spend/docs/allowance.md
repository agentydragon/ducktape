# Flexible allowance

The service can also compute a **single flexible spending allowance**, independently of statement-cycle card totals. Its
`spend-policy.yaml` comes from the private finance-agent repository. Finance Flux reconciles it to a Secret in the
isolated `finance-spend-config` namespace; Ducktape owns an ExternalSecret that copies the named Secret into `plaid-mcp`
for the app. Keep Plaid account IDs in the private file, never in public ducktape. Until the Flux migration has rolled
out, the live app still reads the existing named Secret. Without the policy file the pod will not start. Invalid YAML or
policy fails application startup. Reloader restarts the Deployment after Secret changes; the process reads the file only
at startup. The root `time_zone` is required even when the allowance is disabled. Check the current read-only Plaid
account coverage **before** configuring the allowance.

Generic _synthetic_ YAML example (amounts are integer cents; IDs, categories and prefixes illustrative):

```yaml
time_zone: UTC
cards: []
allowance:
  monthly_minor_units: 100000
  activation_at: 2026-01-31
  spending_account_ids:
    - example-credit-id
    - example-checking-id
  currency: USD
  max_sync_age_hours: 72
  forecast_basis_period_id: rolling_7d
  analysis_categories:
    fixed_housing:
      label: Housing
      color: "#64748B"
    elastic_online_services:
      label: Online services
      color: "#7C3AED"
    excluded_transfer_or_fee:
      label: Transfer or fee
      color: "#94A3B8"
    unclassified:
      label: Unclassified
      color: "#D97706"
  rules:
    - condition:
        type: name_prefix
        field: name
        prefix: EXAMPLE RENT
      kind: fixed
      analysis_category: fixed_housing
    - condition:
        type: category_exact
        field: pfc_detailed
        value: EXAMPLE_TRANSFER_DETAIL
      kind: excluded
      analysis_category: excluded_transfer_or_fee
    - condition:
        type: name_prefix
        field: name
        prefix: EXAMPLE ONLINE
      kind: flexible
      analysis_category: elastic_online_services
      description: Example subscription; keep the classification reviewable.
```

An inclusive `date_range` condition accepts `start`, `end`, or both as ISO dates. Combine it with a merchant condition
and, when needed, `amount_exact` in an `all_of` rule to limit a historical classification without a Plaid transaction ID.

## Explicit full reversal pairs

Use `reversal_pairs` for a confirmed, full posted reversal of a purchase. Each pair has a stable `id`, a `description`,
and two existing transaction conditions: `original` selects the positive debit, and `reversal` selects the negative
credit. Add the list alongside `rules` under `allowance`. `amount_exact` accepts signed amounts, so the two selectors
can require exact opposite values:

```yaml
reversal_pairs:
  - id: example-full-reversal
    description: Synthetic example of a fully reversed purchase.
    original:
      type: all_of
      conditions:
        - type: name_prefix
          field: merchant_name
          prefix: EXAMPLE ORIGINAL SHOP
        - type: amount_exact
          value: "45.00"
        - type: date_range
          start: 2026-01-10
          end: 2026-01-10
    reversal:
      type: all_of
      conditions:
        - type: name_prefix
          field: name
          prefix: EXAMPLE SHOP REVERSAL
        - type: amount_exact
          value: "-45.00"
        - type: date_range
          start: 2026-01-12
          end: 2026-01-12
```

The service resolves a pair only when both selectors identify exactly one posted transaction, the amounts are exact
opposites, and both transactions use the allowance currency and belong to the same account. A transaction cannot be
used by more than one pair; overlaps make the affected pairs unresolved. Pending or future-dated candidates, missing or
ambiguous matches, amount or currency mismatches, and different accounts also leave the pair unresolved. Partial refunds
are not full reversals and must not be paired.

Resolved pairs exclude both legs from the allowance and pace calculations while retaining their ordinary classification
rule and category as context. Statement-cycle card totals remain based on the transactions as received. Unresolved pairs
suppress neither leg: each transaction follows the existing classification and refund handling independently, and the
Spending page explicitly marks affected results provisional. Relevance uses purchase dates and every calculation
dependency, including carryforward since activation. A missing leg has unknown purchase-day scope; its posting-date
selector bounds cannot establish that a report is unaffected. Other reports use the same typed reconciliation
result, resolve against full history before slicing periods, and continue with a provisional numeric result when
possible. Invalid source data or units still block calculation. Review the selectors or transactions before
relying on an unresolved pair's totals. Pair matching is an explicit, conservative exception; it does not infer a
relationship from merchant names or amount alone.

## One rule type, ordered: patterns and one-offs

```yaml
time_zone: UTC
cards: []
allowance:
  monthly_minor_units: 100000
  activation_at: 2026-01-31
  spending_account_ids:
    - example-credit-id
  analysis_categories:
    fixed_housing:
      label: Housing
      color: "#64748B"
    unclassified:
      label: Unclassified
      color: "#D97706"
  rules:
    # A one-off: a fact about one observed transaction, placed above the patterns it corrects.
    - condition:
        type: all_of
        conditions:
          - type: name_prefix
            field: name
            prefix: EXAMPLE HOTEL
          - type: date_range
            start: 2026-06-04
            end: 2026-06-04
          - type: amount_exact
            value: "2430.78"
      kind: fixed
      scope: one_off
      id: example-hotel-2026-06-04
      analysis_category: fixed_housing
      description: Confirmed example stay; date and amount limit this to the observed charge (owner confirmed 2026-10-04).
    # A pattern: a standing classification.
    - condition:
        type: name_prefix
        field: name
        prefix: EXAMPLE RENT
      kind: fixed
      analysis_category: fixed_housing
```

There is **one kind of decision**. A rule is a condition plus a `kind`, and precedence is its position in `rules`:
the first condition that matches governs the transaction, and nothing is checked after it. `scope` states what the
rule claims, and defaults to `pattern`.

`scope: one_off` says "this is true of these observed transactions, not of a category". It is a normal rule with a
stricter shape, enforced when the policy loads: an `all_of` condition carrying a `date_range` with **both** `start`
and `end` spanning at most 31 days, plus at least one name, category, amount, field, or counterparty condition, plus
a stable `id` to cite the correction by and a `description` for why it is right. Placed above the patterns it
corrects, it wins by position; the pattern list stays a list of patterns rather than accumulating exceptions.

A one-off never addresses a transaction by Plaid transaction ID. Relinking an account rewrites those ids, so an
id-addressed correction silently stops applying and quietly restores the wrong classification with no error to show
for it. Write the bounded match instead, and use a `pattern` rule when the classification applies to more than the
handful of transactions inside the window.

Because there is one decision type, one number reports where a classification came from: `rule_number` is a rule's
1-based position in the ordered list, whether it is a pattern or a one-off. Each transaction row also reports the
resolved `effective_kind` — the matched rule's kind, `excluded` for a successfully resolved reversal pair, or null for
an unmatched default-flexible purchase — so the review tally and every display read one server-side answer to "what kind
is this money" instead of re-deriving which tier won. The Configuration tab lists one ordered rule list, with a
**One-off** badge on the entries that carry a bounded match.

Accounting is the same for both scopes: `fixed`/`excluded` leave the allowance, and `flexible` and `review` remain
counted. An `excluded` rule alone does not establish that a negative credit reverses a particular purchase; only a
successfully resolved `reversal_pairs` definition verifies that linkage and excludes both legs. Keep the one-offs short:
they are a ledger of reviewed corrections, not a second rule engine.

When `allowance` is present, it is active. Supply a required `activation_at` ISO date (YYYY-MM-DD) as the stable
credit-cycle anchor; null or omission is invalid. To disable the allowance, omit the entire `allowance` object. Activation
starts at midnight in the required top-level `time_zone`; a purchase assigned to that local day counts in full. Purchase
days follow the timestamp/date hierarchy below. A full monthly credit arrives immediately on activation, again on each
monthly anniversary in the configured time zone (clamped to month-end, always measured from the original day).
Unspent credit carries forward; no
monthly reset or second credit at the first calendar-month boundary. Do not backdate the anchor expecting a clean slate;
choose the intended first credit date. Only transactions dated on or after activation reduce the allowance or appear in
the spending-window totals. For **pace only**, the service also reads the preceding 30 calendar days, applies the same
fixed/excluded/flexible rules and pending replacement handling, and considers positive flexible purchases from before
the activation date. This history is **never imported as opening debt**; the current-cycle, trailing 7/30-day, calendar
and year _spend totals_ still start at activation. The separate `recorded_pace_periods` for `rolling_7d` and
`rolling_30d` are positive recorded purchases in the respective full calendar-day windows divided by 7 and 30 (null on
startup without pace evidence). `forecast.basis_period` identifies the selected rolling window.
`forecast.daily_pace_minor_units` is the potentially higher, early-burst-sensitive projection pace; do not present it as
a literal rolling average. Web, GNOME and CLI show both observed rates against the same approximate
monthly-credit-equivalent daily reference; `spending_signal` compares the selected estimate window's observed rate and
forecast against that **provisional allowance**, not a sustainability guarantee. Web can select the estimate window;
GNOME and CLI use the configured default. Each recorded pace report's `unmatched_charges` count and
amount count positive, default-flexible purchases in the pace lookback, including before activation; this is separate
from the postactivation review tally. Purchase days define membership in each window and the transaction list:
prefer an offset-bearing `authorized_datetime` converted into the configured IANA `time_zone`, then
`authorized_date`, then an offset-bearing posting `datetime` converted to that zone, then the original Plaid `date`.
Date-only values remain unchanged. Missing, malformed, naive, and exact-midnight timestamps are ignored; Plaid
[documents default midnight times](https://plaid.com/docs/api/products/transactions/#transaction-authorized_datetime),
so these do not provide reliable purchase instants. A genuine midnight purchase therefore also falls back to its date.
The original Plaid `date` continues to govern classification `date_range` conditions and card statement totals.
The reader does not bound retrieval by posting date: late postings and UTC midnight crossings must reach the same
purchase-day filter as the allowance calculator. This reads the configured accounts' active transaction history;
if that history grows substantially, optimize with a persisted effective-day index rather than a posting-date bound.
Pending replacement deduplication still uses Plaid's explicit link. Purchase days can change when an institution
adds or corrects authorization information, including on posting; no authorization metadata is inferred from a removed
pending row. A newly linked account with a short historical backfill can understate observed pace; show sync freshness, not a
promise of comprehensive coverage. CLI, web and GNOME show both rolling windows' unmatched counts and summed amounts.

Configured account IDs should cover **all accounts used for purchases** (credit and checking/debit); otherwise this is
not a reliable allowance. If an account is missing, detached from the latest complete account listing, inactive, or
its Item sync exceeds `max_sync_age_hours`, the allowance shows _unavailable_ with no available balance. A fresh sync
of another account on the same Item does not restore coverage, and retained transaction history remains queryable.
Ordered private rules match `name`/`merchant_name` prefixes,
case-insensitive substrings, exact `pfc_primary`/`pfc_detailed` values, or an `all_of` of two or more of those
conditions; first match wins. For a named transfer embedded in a long bank descriptor, combine `name_contains` with an
exact transfer category rather than excluding all wires. **No spending categories are hard-coded.** A purchase with no
matching rule counts as flexible and appears in the review tally; configure exclusions for repayments, income,
transfers, and other non-purchases or they will consume allowance. Plaid category fields are inferences: audit
exclusions and fixed classifications, and prefer names where available. Negative charges are held outside the allowance;
the app does not automatically match credits to earlier flexible purchases. A merchant match alone does not establish
that relationship. Pending transactions are counted, then suppressed if their posted replacement is present; removed
transactions are ignored. Explicit non-USD charges are omitted, so review foreign spending separately before calling the
coverage complete.

The authenticated web page shows available credit, current-cycle carry, calendar month and year-to-date spend, trailing
7/30-day spend, sync timestamp, and a local what-if purchase check (no server-side purchase request). The pace uses the
larger of the configured rolling window's positive flexible purchase average and the since-activation daily average
during that window's first days, so a day-one burst is not diluted by historic quiet days. If no positive flexible
purchase is recorded, pace and projected balance are unavailable until the selected number of activated calendar days
has elapsed; only then is a zero rate evidence for an empty window. Availability remains visible, and an exhausted
balance is still marked exceeded regardless of pace. The exhaustion timestamp uses the pace **without future credits**.
This is a noisy _estimate_, not a prediction or transaction authorization; Plaid sync and transaction posting lag, whose
oldest timestamp is displayed on the primary allowance panel, can hide fresh purchases. The GNOME panel shows the
allowance when active, and links to the cookie-authenticated dashboard for the purchase check; existing card cycles
remain displayed separately. No account limit, card choice, bank controls or automatic recharging is configured by this
PR.
