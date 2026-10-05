# Flexible allowance

The service can also compute a **single flexible spending allowance**, independently of
statement-cycle card totals. The Deployment requires one privately delivered Secret,
`plaid-mcp/plaid-spend-private-config`, with one `config.json` key. The JSON contains
required `cards` and optional `allowance`; omitting `allowance` disables the allowance.
Do not commit this configuration to the public repo; storing it in private git alone
does not deploy it. Without the Secret the pod will not start. Invalid JSON or policy
fails application startup. Reloader restarts the Deployment after the Secret changes; the
process reads the file only at startup.
Check the current read-only Plaid account coverage **before** configuring the allowance.

Generic _synthetic_ example (amounts are integer cents; IDs, categories and prefixes illustrative):

```json
{
  "cards": [],
  "allowance": {
    "monthly_minor_units": 100000,
    "activation_at": "2026-01-31",
    "spending_account_ids": ["example-credit-id", "example-checking-id"],
    "currency": "USD",
    "max_sync_age_hours": 72,
    "rules": [
      { "condition": { "type": "name_prefix", "field": "name", "prefix": "EXAMPLE RENT" }, "kind": "fixed" },
      {
        "condition": { "type": "category_exact", "field": "pfc_detailed", "value": "EXAMPLE_TRANSFER_DETAIL" },
        "kind": "excluded"
      },
      { "condition": { "type": "name_prefix", "field": "name", "prefix": "EXAMPLE ONLINE" }, "kind": "flexible" }
    ]
  }
}
```

When `allowance` is present, it is active. Supply a required `activation_at` ISO date
(YYYY-MM-DD) as the stable credit-cycle anchor; null or omission is invalid. To disable
the allowance, omit the entire `allowance` object. Plaid supplies only
transaction **dates**, not trustworthy purchase times: activation uses the full UTC date, and a purchase dated on activation day counts in full. A full monthly
credit arrives immediately on activation, again on each UTC monthly anniversary
(clamped to month-end, always measured from the original day). Unspent credit carries
forward; no monthly reset or second credit at the first calendar-month boundary.
Do not backdate the anchor expecting a clean slate; choose the intended first credit date. Only transactions dated
on or after activation are counted, and the calendar and year views only span the
activated period.

Configured account IDs should cover **all accounts used for purchases** (credit and
checking/debit); otherwise this is not a reliable allowance. If an account is missing,
inactive, or its sync exceeds `max_sync_age_hours`, the allowance shows _unavailable_
with no available balance. Ordered private rules match `name`/`merchant_name`
prefixes, case-insensitive substrings, exact `pfc_primary`/`pfc_detailed` values, or an `all_of` of two or more of those conditions; first match wins. For a named transfer embedded in a long bank descriptor, combine `name_contains` with an exact transfer category rather than excluding all wires. An `all_of` is not an explicit merchant-name flexible refund match; verify refund handling separately.
**No spending categories are hard-coded.** A purchase with no matching rule counts
as flexible and appears in the review tally; configure exclusions for repayments,
income, transfers, and other non-purchases or they will consume allowance.
Plaid category fields are inferences: audit exclusions and fixed classifications,
and prefer names where available. A negative charge restores room only if an explicit
merchant-name flexible rule matches; otherwise it appears as an unmatched refund.
Pending
transactions are counted, then suppressed if their posted replacement is present;
removed transactions are ignored. Explicit non-USD charges are omitted, so review
foreign spending separately before calling the coverage complete.

The authenticated web page shows available credit, current-cycle carry, calendar
month and year-to-date spend, trailing 7/30-day spend, sync timestamp, and a local
what-if purchase check (no server-side purchase request). An early warning compares
7-day daily positive spend pace against remaining days until the next credit; the
exhaustion timestamp uses that pace **without future credits**. This
is a noisy _estimate_, not a forecast or transaction authorization. The GNOME panel
shows the allowance when active, and links to the cookie-authenticated dashboard for
the purchase check; existing card cycles remain displayed separately. No account
limit, card choice, bank controls or automatic recharging is configured by this PR.
