# Flexible allowance

The service can also compute a **single flexible spending allowance**, independently of statement-cycle card totals. Its
`spend-policy.yaml` comes from the private finance-agent repository. Finance Flux reconciles it to a Secret in the
isolated `finance-spend-config` namespace; Ducktape owns an ExternalSecret that copies the named Secret into `plaid-mcp`
for the app. Keep Plaid account IDs in the private file, never in public ducktape. Until the Flux migration has rolled
out, the live app still reads the existing named Secret. Without the policy file the pod will not start. Invalid YAML or
policy fails application startup. Reloader restarts the Deployment after Secret changes; the process reads the file only
at startup. Check the current read-only Plaid account coverage **before** configuring the allowance.

Generic _synthetic_ YAML example (amounts are integer cents; IDs, categories and prefixes illustrative):

```yaml
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

## One-off overrides

```yaml
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
    - condition:
        type: name_prefix
        field: name
        prefix: EXAMPLE RENT
      kind: fixed
      analysis_category: fixed_housing
  overrides:
    - id: example-hotel-2026-06-04
      match:
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
      analysis_category: fixed_housing
      note: Confirmed example stay; date and amount limit this to the observed charge (owner confirmed 2026-10-04).
```

An `override` corrects **one observed transaction** while `rules` describe recurring patterns. Overrides are checked
first, so a correction always wins over a rule that also matches. They address a transaction by **match** — a bounded
`all_of` requiring a `date_range` with both `start` and `end` (at most 31 days) plus at least one name, category,
amount, field, or counterparty condition — and never by Plaid transaction ID: relinking an account rewrites those ids
and silently dangles any id-addressed correction, restoring the wrong classification without an error. Use an `all_of`
rule instead when the classification applies to more than the handful of transactions inside that window. `id` is a
stable human-written key shown with each matched transaction, and `note` records why, ideally who confirmed it and
when. Overrides reuse the same `kind` values and `analysis_category` catalog as rules, so an overridden transaction is
accounted for exactly like a rule-classified one: `fixed`/`excluded` leave the allowance, `flexible` and `review`
remain counted, and a negative credit addressed by an `excluded` override is a paired transfer leg rather than an
unmatched refund. The Configuration tab lists overrides separately from rules, and the Transactions tab names the
applied override and its note in place of a rule number. Keep the override list short: it is a ledger of reviewed
exceptions, not a second rule engine.

When `allowance` is present, it is active. Supply a required `activation_at` ISO date (YYYY-MM-DD) as the stable
credit-cycle anchor; null or omission is invalid. To disable the allowance, omit the entire `allowance` object. Plaid
supplies only transaction **dates**, not trustworthy purchase times: activation uses the full UTC date, and a purchase
dated on activation day counts in full. A full monthly credit arrives immediately on activation, again on each UTC
monthly anniversary (clamped to month-end, always measured from the original day). Unspent credit carries forward; no
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
from the postactivation review tally. Plaid's transaction date (not an exact swipe timestamp) defines membership in each
window. A newly linked account with a short historical backfill can understate observed pace; show sync freshness, not a
promise of comprehensive coverage. CLI, web and GNOME show both rolling windows' unmatched counts and summed amounts.

Configured account IDs should cover **all accounts used for purchases** (credit and checking/debit); otherwise this is
not a reliable allowance. If an account is missing, inactive, or its sync exceeds `max_sync_age_hours`, the allowance
shows _unavailable_ with no available balance. Ordered private rules match `name`/`merchant_name` prefixes,
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
