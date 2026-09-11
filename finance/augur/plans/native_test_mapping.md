# Native financial test cutover map

Inventory of 95 declarations from baseline `fe377a2764`, all mapped below to
Python coverage. Rust paths identify removed baseline code, not current imports.
The Rust identifiers below refer to the removed baseline; the live counterparts are Python tests.

Generated arithmetic properties use Hypothesis. Their primary rounding oracle multiplies
the result back and checks distance and tie direction, not another division implementation.
The quantity/money equivalence case now checks that single count operation against this
independent oracle; Python does not introduce redundant wrappers for identical arithmetic.
Wide and narrow symmetry examples share one parameterized Python test.

## `rust/engine/actors_test.rs`

- [x] `cash_only_actor_observes_and_purchases_an_unheld_declared_asset` → `sim/test_world.py::test_cash_only_actor_observes_and_purchases_an_unheld_declared_asset`
- [x] `declaring_an_empty_pool_does_not_invest_cash_without_an_action` → `sim/test_world.py::test_declaring_an_empty_pool_does_not_invest_cash_without_an_action`
- [x] `an_empty_pool_purchase_rejects_wrong_account_or_scale_without_mutation` → `sim/test_world.py::test_an_empty_pool_purchase_rejects_wrong_account_or_scale_without_mutation`
- [x] `declarations_reject_missing_prices_and_do_not_fall_back_to_initial_lots` → `sim/test_world.py::test_declarations_reject_missing_prices_and_do_not_fall_back_to_initial_lots`
- [x] `cashflows_claims_sales_and_cross_year_tax_share_financial_books` → `sim/test_world.py::test_cashflows_claims_sales_and_cross_year_tax_share_financial_books`
- [x] `ordered_actions_can_buy_before_transferring_and_buy_again` → `sim/test_world.py::test_ordered_actions_can_buy_before_transferring_and_buy_again`
- [x] `rejected_financial_request_preserves_prior_sale_and_independent_world` → `sim/test_world.py::test_rejected_financial_request_preserves_prior_sale_and_independent_world`
- [x] `payment_capture_names_the_actual_selected_source` → `sim/test_world.py::test_payment_capture_names_the_actual_selected_source`
- [x] `compact_capture_replays_observed_prefixes_and_canonical_payment_identity` → `sim/test_world.py::test_compact_capture_replays_observed_prefixes_and_canonical_payment_identity`
- [x] `unpaid_claims_keep_occurrence_and_source_without_hidden_sales` → `sim/test_world.py::test_unpaid_claims_keep_occurrence_and_source_without_hidden_sales`

## `rust/engine/components_test.rs`

- [x] `basis_statement_cash_and_tax_reconcile_without_ordinary_lots` → `sim/test_managed.py::test_basis_statement_cash_and_tax_reconcile_without_ordinary_lots`
- [x] `invalid_effects_and_overflow_leave_every_financial_book_unchanged` → `sim/test_managed.py::test_invalid_effects_and_overflow_leave_every_financial_book_unchanged`
- [x] `distribution_cash_uses_interest_source_not_capital_gain_journal_account` → `sim/test_managed.py::test_distribution_cash_uses_interest_source_not_capital_gain_journal_account`
- [x] `withdrawal_receipt_does_not_recalculate_component_rounded_value` → `sim/test_managed.py::test_withdrawal_receipt_does_not_recalculate_component_rounded_value`
- [x] `component_capture_keeps_explicit_stop_marks_and_independent_books` → `sim/test_managed.py::test_component_capture_keeps_explicit_stop_marks_and_independent_books`
- [x] `opening_component_rows_require_exact_portfolio_coverage` → `sim/test_managed.py::test_opening_component_rows_require_exact_portfolio_coverage`
- [x] `zero_component_marks_do_not_relax_ordinary_quote_or_negative_mark_validation` → `sim/validation_test.py::test_zero_price_is_allowed_only_for_exclusively_managed_assets`

## `rust/engine/mortgages_test.rs`

- [x] `mortgage_postings_use_selected_cash_and_ledger_principal_through_payoff` → `sim/test_world_mortgages.py::test_mortgage_postings_use_selected_cash_and_ledger_principal_through_payoff`
- [x] `invalid_mortgage_effects_do_not_change_cash_or_principal` → `sim/test_world_mortgages.py::test_invalid_mortgage_effects_do_not_change_cash_or_principal`

## `rust/engine/payments_test.rs`

- `claim_occurrences_are_not_labels_and_consumption_is_not_a_claim` → `sim/test_payments.py::test_claim_occurrences_are_not_labels_and_consumption_is_not_a_claim` (passed in focused RBE run)
- `rejected_payments_change_neither_books_nor_capture` → `sim/test_payments.py::test_rejected_payments_change_neither_books_nor_capture` (passed in focused RBE run)
- `moving_cash_within_the_actor_is_not_paid_consumption` → `sim/test_payments.py::test_moving_cash_within_the_actor_is_not_paid_consumption` (passed in focused RBE run)

## `rust/engine/private_equity_test.rs`

- [x] `recovery_total_one_for_three_units_is_not_rounded_to_zero` → `sim/test_private_equity.py::test_recovery_total[total_one_for_three_units]`
- [x] `recovery_total_two_for_three_units_is_not_rounded_to_three` → `sim/test_private_equity.py::test_recovery_total[total_two_for_three_units]`
- [x] `recovery_total_is_apportioned_across_lots_and_accounts` → `sim/test_private_equity.py::test_recovery_total[across_lots_and_accounts]`
- [x] `recovery_uses_economic_units_across_different_account_scales` → `sim/test_private_equity.py::test_recovery_total[economic_units_across_scales]`
- [x] `recovery_cashout_applies_to_the_remaining_position_after_an_earlier_sale` → `sim/test_private_equity.py::test_recovery_cashout_applies_to_the_remaining_position_after_an_earlier_sale`

## `rust/engine/tests.rs`

- [x] `scoped_observations_match_output_at_same_marks_and_round_each_lot` → `sim/test_observations.py::test_scoped_observations_match_output_at_same_marks_and_round_each_lot`
- [x] `actor_books_do_not_read_future_prices_or_cpi` → `sim/test_observations.py::test_actor_books_do_not_read_future_prices_or_cpi`
- [x] `actor_books_follow_partial_sales_and_hide_exhausted_lots` → `sim/test_observations.py::test_actor_books_follow_partial_sales_and_hide_exhausted_lots`
- [x] `actor_books_reject_unpriced_public_positions_before_inspection` → `sim/test_observations.py::test_actor_books_reject_unpriced_public_positions_before_inspection`
- [x] `retained_rollouts_keep_opening_books_lots_and_tax_state_independent` → `sim/test_world.py::test_retained_rollouts_keep_opening_books_lots_and_tax_state_independent`
- [x] `month_stepping_preserves_tax_year_and_stopped_books_in_every_capture_mode` → `sim/test_world.py::test_month_stepping_preserves_tax_year_and_stopped_books_in_every_capture_mode`
- [x] `claim_views_keep_assembled_amount_identity_and_payer_scope` → `sim/test_observations.py::test_claim_views_keep_assembled_amount_identity_and_payer_scope`
- [x] `rejects_invalid_fixture_metadata` → `sim/test_validation_contracts.py::test_rejects_invalid_fixture_metadata`
- [x] `series_indexed_amounts_follow_rollout_specific_reset_boundaries` → `sim/testing/indexed_payments_test.py::test_series_indexed_recurring_rent_obligation_resets_yearly_by_rollout`
- [x] `series_indexed_amount_validation_rejects_invalid_paths` → `sim/testing/indexed_payments_test.py::{test_series_indexed_amount_cannot_fire_before_base_month,test_series_indexed_amount_requires_external_series_coverage,test_series_indexed_amount_rejects_zero_base_level}`
- [x] `bond_principal_remains_until_redemption_event` → `sim/test_held_bonds.py::{test_no_month_zero_coupon_and_redemption_keeps_the_maturity_coupon,test_stopped_bond_snapshot_uses_the_last_observed_index}`
- [x] `nominal_and_indexed_bonds_follow_coupon_redemption_and_accretion_contracts` → `sim/test_held_bonds.py::test_tips_deflation_changes_income_but_redemption_has_a_face_floor`
- [x] `bond_validation_rejects_non_par_and_missing_index_paths` → `sim/test_validation_contracts.py::test_bond_validation_rejects_non_par_and_missing_index_paths`
- [x] `rejects_invalid_references_before_rollout_execution` → `sim/test_validation_contracts.py::test_rejects_invalid_references_before_rollout_execution`
- [x] `rejects_income_from_a_source_the_scenario_did_not_declare` → `sim/test_validation_contracts.py::test_rejects_income_from_a_source_the_scenario_did_not_declare`
- [x] `distribution_tax_character_requires_a_complete_known_issuer_split` → `sim/test_validation_contracts.py::test_distribution_tax_character_requires_a_complete_known_issuer_split`
- [x] `rejects_invalid_property_contracts_before_rollout_execution` → `sim/test_validation_contracts.py::test_rejects_invalid_property_contracts_before_rollout_execution`
- [x] `rejects_mixed_quantity_scales_and_invalid_security_prices` → `sim/test_validation_contracts.py::test_rejects_mixed_quantity_scales_and_invalid_security_prices`
- [x] `zero_distribution_is_valid_but_negative_distribution_and_zero_price_are_not` → `sim/test_validation_contracts.py::test_zero_distribution_is_valid_but_negative_distribution_and_zero_price_are_not`
- [x] `transfer_and_fifo_sale_remain_balanced` → `sim/test_world.py::test_transfer_and_fifo_sale_remain_balanced`
- [x] `mid_horizon_property_mark_and_sale_share_the_purchase_anchor` → `sim/test_world_mortgages.py::test_mid_horizon_property_mark_and_sale_share_the_purchase_anchor`
- [x] `oversell_is_rejected_before_any_disposition` → `sim/test_holdings.py::test_oversell_is_rejected_before_any_disposition`
- [x] `failure_stops_future_actions_and_preserves_the_observed_book` → `sim/testing/obligations_test.py::test_failed_path_skips_future_transfers_and_policy_calls_while_other_path_continues`
- [x] `same_source_recurring_obligations_settle_all_or_none` → `sim/test_payments.py::{test_grouped_funding_is_decided_before_incoming_claim_payments,test_funded_group_does_not_rescue_a_source_that_was_unfunded_at_preflight}`

## `rust/engine/trades_test.rs`

- `exact_selection_is_not_fifo_and_full_lot_basis_reconciles` → `sim/test_holdings.py::test_exact_selection_is_not_fifo_and_full_lot_basis_reconciles` (passed in focused RBE run)
- `total_proceeds_use_the_same_basis_and_tax_commit` → `sim/test_holdings.py::test_total_proceeds_use_the_same_basis_and_tax_commit` (passed in focused RBE run)
- `rejected_total_cashouts_leave_lots_cash_tax_and_capture_unchanged` → `sim/test_holdings.py::test_rejected_total_cashouts_leave_lots_cash_tax_and_capture_unchanged` (passed in focused RBE run)
- `fifo_scheduled_sale_matches_the_same_explicit_selection` → `sim/test_holdings.py::test_fifo_scheduled_sale_matches_the_same_explicit_selection` (passed in focused RBE run)
- `invalid_exact_lot_requests_leave_every_book_unchanged` → `sim/test_holdings.py::test_invalid_exact_lot_requests_leave_every_book_unchanged` (passed in focused RBE run)
- `overflow_after_first_lot_or_jurisdiction_cannot_partially_commit` → `sim/test_holdings.py::test_overflow_after_first_lot_or_jurisdiction_cannot_partially_commit` (passed in focused RBE run)
- `rejected_scheduled_sale_preserves_every_book` → `sim/test_holdings.py::test_rejected_scheduled_sale_preserves_every_book` (passed in focused RBE run)
- `purchase_posts_cash_and_basis_then_joins_future_exact_sales` → `sim/test_holdings.py::test_purchase_posts_cash_and_basis_then_joins_future_exact_sales` (passed in focused RBE run)
- `invalid_or_unfunded_purchase_does_not_create_lot_or_debit_cash` → `sim/test_holdings.py::test_invalid_or_unfunded_purchase_does_not_create_lot_or_debit_cash` (passed in focused RBE run)

## `rust/engine/transfers_test.rs`

- `admitted_actor_transfer_matches_scheduled_accounting_exactly` → `sim/test_accounting.py::test_admitted_actor_transfer_matches_scheduled_accounting_exactly` (passed in focused RBE run)
- `scheduled_income_can_arrive_from_an_exogenous_negative_balance` → `sim/test_accounting.py::test_scheduled_income_can_arrive_from_an_exogenous_negative_balance` (passed in focused RBE run)
- `actors_cannot_overdraw_or_impersonate_another_source_or_classify_tax` → `sim/test_accounting.py::test_actors_cannot_overdraw_or_impersonate_another_source_or_classify_tax` (passed in focused RBE run)
- `scheduled_tax_and_posting_failures_do_not_partially_apply` → `sim/test_accounting.py::test_scheduled_tax_and_posting_failures_do_not_partially_apply` (passed in focused RBE run)
- `shared_income_row_is_updated_in_order_without_overwriting_a_prior_change` → `sim/test_accounting.py::test_shared_income_row_is_updated_in_order_without_overwriting_a_prior_change` (passed in focused RBE run)
- `transfer_sequence_is_not_an_implicitly_atomic_batch` → `sim/test_accounting.py::test_transfer_sequence_is_not_an_implicitly_atomic_batch` (passed in focused RBE run)

## `rust/execution.rs`

- [x] `money_crosses_the_wire_only_as_an_integer` → `sim/testing/test_invocation.py::{test_file_decode_rejects_invalid_prepared_facts,test_prepared_input_retains_original_path_cpi_and_selected_replay}`

## `rust/ledger.rs`

- `compound_entry_balances_and_applies_atomically` → `sim/test_ledger.py::test_compound_entry_balances_and_applies_atomically` (passed in focused RBE run)
- `rejects_unbalanced_entry_without_mutation` → `sim/test_ledger.py::test_rejects_unbalanced_entry_without_mutation` (passed in focused RBE run)
- `repeated_account_postings_are_accumulated_before_mutation` → `sim/test_ledger.py::test_repeated_account_postings_are_accumulated_before_mutation` (passed in focused RBE run)

## `rust/money.rs`

- `half_up_rounding_is_symmetric` → `sim/test_money.py::test_half_up_rounding_is_symmetric` (passed in focused RBE run)
- `wide_half_up_rounding_is_symmetric` → `sim/test_money.py::test_half_up_rounding_is_symmetric` (passed in focused RBE run)
- `a_rate_of_whole_quanta_per_unit_agrees_with_a_price` → `sim/test_money.py::test_a_rate_of_whole_quanta_per_unit_agrees_with_a_price` (passed in focused RBE run)
- `a_rate_below_one_quantum_per_unit_still_comes_to_money` → `sim/test_money.py::test_a_rate_below_one_quantum_per_unit_still_comes_to_money` (passed in focused RBE run)
- `the_product_is_formed_before_either_scale_divides_out` → `sim/test_money.py::test_the_product_is_formed_before_either_scale_divides_out` (passed in focused RBE run)
- `a_gwei_scaled_position_does_not_overflow_the_denominator` → `sim/test_money.py::test_a_gwei_scaled_position_does_not_overflow_the_denominator` (passed in focused RBE run)

## `rust/money_proptest.rs`

- `narrow_mul_div_rounds_half_away_from_zero` → `sim/test_money.py::test_narrow_mul_div_rounds_half_away_from_zero` (passed in focused RBE run)
- `an_exact_tie_rounds_away_from_zero` → `sim/test_money.py::test_an_exact_tie_rounds_away_from_zero` (passed in focused RBE run)
- `narrow_mul_div_is_sign_symmetric` → `sim/test_money.py::test_narrow_mul_div_is_sign_symmetric` (passed in focused RBE run)
- `a_zero_denominator_is_refused` → `sim/test_money.py::test_a_zero_denominator_is_refused` (passed in focused RBE run)
- `wide_mul_div_rounds_half_away_from_zero` → `sim/test_money.py::test_wide_mul_div_rounds_half_away_from_zero` (passed in focused RBE run)
- `apportioning_everything_moves_everything` → `sim/test_money.py::test_apportioning_everything_moves_everything` (passed in focused RBE run)
- `equal_factors_scale_money_identically` → `sim/test_money.py::test_equal_factors_scale_money_identically` (passed in focused RBE run)
- `a_factor_and_its_complement_split_an_amount` → `sim/test_money.py::test_a_factor_and_its_complement_split_an_amount` (passed in focused RBE run)
- `a_quantity_scales_like_money` → `sim/test_money.py::test_a_quantity_scales_like_money` (passed in focused RBE run)
- `only_powers_of_ten_are_quantity_scales` → `sim/test_money.py::test_only_powers_of_ten_are_quantity_scales` (passed in focused RBE run)
- `a_scale_that_is_not_a_power_of_ten_is_refused` → `sim/test_money.py::test_a_scale_that_is_not_a_power_of_ten_is_refused` (passed in focused RBE run)
- `a_rate_spread_over_periods_re_totals` → `sim/test_money.py::test_a_rate_spread_over_periods_re_totals` (passed in focused RBE run)
- `liquidating_a_lot_consumes_exactly_its_basis` → `sim/test_money.py::test_liquidating_a_lot_consumes_exactly_its_basis` (passed in focused RBE run)

## `rust/tax.rs`

- `bracket_tax_rounds_aggregate_once` → `sim/test_tax.py::test_bracket_tax_rounds_aggregate_once` (passed in focused RBE run)
- `preferential_gain_stacks_above_ordinary_income` → `sim/test_tax.py::test_preferential_gain_stacks_above_ordinary_income` (passed in focused RBE run)
- `section_1250_uses_incremental_brackets_below_the_rate_cap` → `sim/test_tax.py::test_section_1250_uses_incremental_brackets_below_the_rate_cap` (passed in focused RBE run)
- `losses_cross_net_and_carry_forward` → `sim/test_tax.py::test_losses_cross_net_and_carry_forward` (passed in focused RBE run)
- `capital_gain_netting_reports_overflow` → `sim/test_tax.py::test_capital_gain_netting_reports_overflow` (passed in focused RBE run)
- `rejects_negative_rule_amounts` → `sim/test_tax.py::test_rejects_negative_rule_amounts` (passed in focused RBE run)

## Retained coverage and evidence

All 95 native declarations have Python counterparts above. Existing Python tests
formerly under `rust/` are retained under `sim/testing/`; artifact helpers live
in `sim/artifacts.py`. The native source, extension, stubs and private codecs are
removed. This mapping preserves review traceability, not a second implementation.

- Money properties and ledger/mortgage/TLH controls:
  [89156cfe](https://app.buildbuddy.io/invocation/89156cfe-8c37-456a-b927-7903cee4b966).
- Tax assessment: [d1b76b65](https://app.buildbuddy.io/invocation/d1b76b65-08ae-436a-8930-832346db4712).
- Actor/component/mortgage/PE ports and financial regression:
  [c201954e](https://app.buildbuddy.io/invocation/c201954e-5805-4af4-9e46-6cf60626057e),
  32 targets and 560 pytest cases, no failures/errors/skips.
- Observation/admission/artifact ports:
  [f8238d06](https://app.buildbuddy.io/invocation/f8238d06-3ba9-465d-8c5e-dbf2da4fe312).
- Reconciled final-five tests with complete stopped-result/event immutability:
  [a576392f](https://app.buildbuddy.io/invocation/a576392f-b4b0-4588-8281-de004dada587).

These are checkpoint-specific results. Full native-free consumer validation and
its exact head are reported in the cutover PR rather than inferred from them.
