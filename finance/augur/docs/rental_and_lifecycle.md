# Rental And Property Lifecycle

Current-state notes for Augur's owned-property rental, primary-residence, and sale
modeling. The original implementation plan lived in `augur/plans/`; the plan is now
mostly shipped, so this doc keeps only behavior and remaining gotchas.

## Product Surface

Owned-property rental starts at purchase through `PropertyPurchase.initial_rental`.
If it is unset, the property has no tenant rent stream; a later
`set_rented_fraction` event with a positive fraction is rejected because the
scenario has no rent/vacancy terms to resize.

`RentalIncomePlan.full_property_monthly_rent_usd` is full-property market rent before
vacancy and management fees. If it is `None`, product lowering uses the selected
property's `rent_estimate_usd`. Collected tenant rent is:

```text
full_property_monthly_rent * fraction_rented * (1 - vacancy_pct)
```

This means a $6,000 full-property rent with `fraction_rented = 0.5` and
`vacancy_pct = 0.10` wires $2,700/month of tenant income.

`RentalManagement.management_fee_pct` is charged against collected tenant rent.
`RentalManagement.leasing_fee_months` is charged against rent for the leased portion,
before vacancy:

```text
full_property_monthly_rent * fraction_rented * leasing_fee_months
```

Both tenant rent and agency fees index annually by the property's `rent:<location_id>`
series.

`PropertyPurchase.is_primary_residence` does two separate things at purchase time:

- creates the initial sim-side primary-residence assignment for the primary agent;
- if the purchase is mortgaged, creates the mortgage-interest deduction policy.

Mid-horizon lifecycle events are scoped to the purchased property in product wire:

- `set_rented_fraction`: mutates sim runtime `rented_fraction`;
- `set_primary_residence`: assigns or clears the primary agent's main-home property;
- `capital_improvement`: debits cash and increases depreciable building basis;
- `property_sale`: sells the property, pays off mortgage debt, and freezes later
  property activity.

Product lowering turns the effective rented-fraction timeline into tenant-rent and
agency-fee property cashflows. `set_rented_fraction` events resize, stop, or
restart those cashflows at the start of their event month. Sale stops rental
cashflows in the sale month.

## Simulator Behavior

The sim-level scenario separates property use from property ownership:

- `ScheduledPropertyPurchase.rented_fraction` is the initial rented share.
- `Scenario.initial_primary_residences` is agent-scoped, one initial main home per agent.
- `Scenario.primary_residence_events` assigns or clears an agent's main home over time.
- `Scenario.property_lifecycle_events` handles rented-fraction changes, improvements,
  and sales.
- `Scenario.scheduled_property_cashflows` and
  `Scenario.recurring_property_cashflows` model property-linked rent,
  management, and leasing cashflows. The engine gates them by property
  ownership lifecycle, then decodes fired rows into the generic transfer event
  frame without adding `property_id` to transfer events.

At runtime, the engine carries mutable per-property `rented_fraction` and building basis
buffers. Within each month, primary-residence events fire first, then property
lifecycle events, then generic transfers, property purchases, property cashflows,
asset sales, obligations, owner-occupied-month accrual, depreciation, and tax
accrual. This means a same-month primary-residence event can fire before a sale,
but the sale clears the assignment and the sold property does not accrue an
owner-occupied month. Same-property `PropertySaleEvent` cannot share a month with
`SetRentedFractionEvent` or `CapitalImprovementEvent`; those combinations are
rejected because sale basis, depreciation, and rental routing would otherwise be
ambiguous.

Schedule E and owner-use splits read the runtime rented fraction:

- rental income is ordinary income;
- management, leasing, HOA, insurance, maintenance, property-tax rented share,
  mortgage-interest rented share, and depreciation can deduct against ordinary income;
- owner-share mortgage interest flows through MID;
- owner-share property tax flows through federal SALT.

Section 121 qualifying-use months accrue only when all are true:

- the property is active;
- the property's owning agent has that property assigned as primary residence;
- the property is not fully rented.

On sale, the engine looks back 60 months and applies the profile's Section 121 cap when
there are at least 24 qualifying months. Only single-filer $250k is wired today; adding
other filing statuses is intentionally loud in the tax compiler.

Property sale computes market value from the property's `home_value:<location_id>`
series, pays off attached mortgage principal, computes realized gain, separates Section
1250 recapture, applies Section 121 to post-recapture gain, then routes the remainder to
long-term capital gain. Federal-style Section 1250 uses the lesser of the implied ordinary
marginal tax and the 25% cap; state-style links treat recapture as ordinary income.

## Frontend

The property rental panel exposes:

- fraction rented;
- vacancy;
- full-property monthly rent;
- optional property management settings.

The lifecycle editor writes `PropertyPurchase.lifecycle_events` and packs them in the
separate `lc` URL parameter. The positional `s` URL state keeps the rent override slot in
the same position as the older ambiguous field, but the state key is now
`rentalFullPropertyMonthlyUsd`.

Rollout markers include rented-fraction changes, primary-residence changes, capital
improvements, and property sales.

## Known Gaps

The user's outside-rent obligation is still flat for the horizon:
`ScenarioKey.monthly_rent_usd` plus `rental_location_id`. There is no product-level
timeline event for changing outside rent. That should be explicit housing-cost state
for the user, not implicit behavior derived only from owned-property primary-residence
assignment.

Section 121 still lacks non-qualified-use proration and one-sale-per-24-months tracking.
Only the single-filer exclusion cap is configured.

Stochastic tenant vacancy and individual tenant turnover are not modeled. Vacancy is a
deterministic scalar and leasing fees use an average-tenancy cadence.

## Verification

Useful focused targets:

```bash
bazelisk test --config=rbe //augur/product:service_test //augur/api:config_test //augur/api:catalog_test //augur:browser_shell_test --test_output=errors
bazelisk test --config=rbe //augur/sim:simulate_test //augur/sim:test_rental_lifecycle_e2e --test_output=errors
```
