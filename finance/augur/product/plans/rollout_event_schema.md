# Rollout event schema

Plan, not implemented. A clean-room wire schema for the rollout events (`RolloutEvent` in `product/wire.py`). The wire needs no backward compatibility. Open decisions are at the end. When the schema lands, `wire.py` is the reference and this file goes.

## What we represent

The simulator records one frame per fact type (`sim/events.py`). The wire projects the primary agent's rows into `RolloutEvent`s (`product/projection.py`). A timeline of one rollout has to say the following.

| Group          | What happened                                                                 | Simulator record                                                                                                         | What the wire carries today                                                                                                                                                                                                                                              |
| -------------- | ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Obligations    | A bill came due and was paid in full or not                                   | `obligation_settlements` (`obligation_type`: 10 values)                                                                  | 7 kinds: `monthly_expense`, `outside_rent`, `property_tax_payment`, `hoa_dues_payment`, `homeowners_insurance_payment`, `property_maintenance_payment`, `tax_payment` (8 of the 10 types); each repeats `amount_due_quanta`, `amount_paid_quanta` and `shortfall_quanta` |
| Failure        | A due obligation could not be paid; the rollout stops                         | `rollout_failures` (also has `obligation_type`)                                                                          | `failure`, amounts only; the obligation type is dropped                                                                                                                                                                                                                  |
| Tax year       | A year's tax was computed per jurisdiction                                    | `tax_accruals` + `tax_breakdowns` (14 amount columns)                                                                    | `tax_accrual`, 11 fields (9 of the 14 amounts)                                                                                                                                                                                                                           |
| Securities     | Units sold, TLH portfolio effects, private-equity marks and tenders           | `lot_dispositions`, `tlh_financial_effects`, `private_equity_events`, `private_equity_opportunities`                     | `holding_sale`, `tlh_financial_effect`, `private_equity_event`, `private_equity_opportunity`                                                                                                                                                                             |
| Property money | Purchase, closing costs, mortgage origination and payments, sale, improvement | `property_purchases`, `mortgage_originations`, `mortgage_payments`, `property_sale_events`, `capital_improvement_events` | `property_purchase` (+ a separate `closing_cost_payment`), `mortgage_payment`, `property_sale`, `capital_improvement`; origination only as `mortgage_principal_quanta`                                                                                                   |
| Property state | Rented share or primary residence changed                                     | `set_rented_fraction_events`, `set_primary_residence_events`                                                             | `set_rented_fraction`, `set_primary_residence`                                                                                                                                                                                                                           |

Not events today, and not proposed here: transfers (wages, `income_category`).

The simulator records these facts and the wire drops them: the obligation type on a failure; sale commission, escrow/title and transfer tax; mortgage rate, term and monthly payment; the improvement description; purchase transfer tax and adjusted basis.

## Today's shape

`RolloutEvent` is a union of 20 classes on `kind`, all subclasses of `_RolloutEventBase` (`month_index`, `amount_quanta`). Only `month_index` is common to every kind. `amount_quanta` is a made-up `"0"` on 2 kinds (`set_rented_fraction`, `set_primary_residence`), a copy of a field the kind already has on 13, and on the other 5 the kind's only amount or a sum of its fields. The event table prints it for every row.

## Principles

1. Facts on the wire, presentation in the UI. Labels, colours, legend rows and the "headline amount" of a row belong to the frontend.
2. Common fields are the ones every kind really has: `month_index`.
3. One shape per concept. Kinds that differ only by an enum value are one kind with that enum.
4. Each amount has one name that says what it is.
5. Typed ids and enums throughout, as today.

## The schema (13 kinds, down from 20)

`RolloutEvent = { month_index, detail }`, where `detail` is a tagged union on `kind`.

| `kind`                       | Fields                                                                                                                               |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `obligation_settlement`      | `obligation_type` (enum), `amount_due_quanta`, `amount_paid_quanta`, `shortfall_quanta`                                              |
| `rollout_failure`            | the same four fields; `obligation_type` says what could not be paid                                                                  |
| `tax_accrual`                | `jurisdiction_id`, `tax_year_end_month`, income, deduction, tax and total fields as today                                            |
| `holding_sale`               | `asset`, `asset_label`, `units`, `proceeds_quanta`, `cost_basis_quanta`                                                              |
| `tlh_financial_effect`       | ids, `operation`, `cash_quanta` (signed; was `amount_quanta`), gains, basis change, income                                           |
| `private_equity_event`       | as today                                                                                                                             |
| `private_equity_opportunity` | as today                                                                                                                             |
| `property_purchase`          | `property_id`, price, down payment, mortgage principal, closing cost (absorbs `closing_cost_payment`); optionally the mortgage terms |
| `mortgage_payment`           | `interest_quanta`, `principal_quanta` (the total is their sum, computed in the UI)                                                   |
| `property_sale`              | as today, plus commission, escrow/title and transfer tax                                                                             |
| `capital_improvement`        | `property_id`, `improvement_quanta` (was `amount_quanta`), optionally `description`                                                  |
| `set_rented_fraction`        | `property_id`, `rented_fraction`                                                                                                     |
| `set_primary_residence`      | `agent_id`, `property_id`, `is_primary_residence`                                                                                    |

Gone: `_RolloutEventBase`, the envelope `amount_quanta`, the seven obligation classes (replaced by one), the closing-cost class.

## Frontend consequences

- Formatters take the `detail` (plus currency) instead of the whole event, so `EventOfKind<K>` becomes `Extract<Detail, { kind: K }>` and needs no `Omit`. That is why the nested shape is cheap; with `amount_quanta` in the envelope it was not.
- `EVENT_FORMATTERS` gets a per-kind `amount` next to `label` and `detail`. Markers return nothing, so their amount cell is empty.
- Legend rows, colours and hidden defaults stay as they are. They key on a UI category: `kind`, or `obligation_type` for obligations. `ROLLOUT_EVENT_KIND_ORDER` keeps ordering same-month events: by kind, then obligation type.
- Size, counted on `devel`: `projection.py` builds 14 events directly and 6 through `payments()` (these collapse to one); 48 test lines in 4 files; about 90 TS lines in 5 files. One PR. The OpenAPI JSON and the Zod file are generated at build time.

## Open decisions

1. Nested `{month_index, detail}`, or flat with `month_index` on each kind? Proposed: nested.
2. One `obligation_settlement` with `obligation_type`? Proposed: yes.
3. Fold the closing cost into `property_purchase` (one table row fewer)? Open.
4. Which of the dropped facts should the UI show: the failure's obligation type, sale costs, mortgage terms, the improvement description? Proposed: the first two.
5. Should wages and other transfers ever appear in the timeline? Not proposed.
6. Rename `set_rented_fraction` and `set_primary_residence` to past tense (`rented_fraction_changed`, `primary_residence_changed`)? The names read like commands; they are events. Open.

Not examined: consumers outside `finance/augur`.
