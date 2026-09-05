"""Small test-only projections from the canonical dense simulator output.

Production no longer exposes these long-form state mirrors. Behavioral tests still use
compact row-oriented assertions where a state trajectory is the subject, so these helpers
make the array-axis and plan-code lookups explicit without restoring a production facade.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl


def _parts(run: Any) -> tuple[Any, Any]:
    return run.plan, run.output


def _last_reported_month(plan: Any, output: Any) -> np.ndarray:
    """The last month each rollout has anything to report, by rollout index.

    A rollout that runs out of cash stops there; the failure month itself still reports, so
    that the failure does. `codec/plan.py` states the same rule for the event log.
    """

    failed_month = np.asarray(output.state.failed_month[plan.horizon_months], dtype=np.int64)
    return np.where(failed_month < 0, np.int64(plan.horizon_months), failed_month)


def _strings(plan: Any, codes: np.ndarray) -> np.ndarray:
    strings = plan.strings
    flat = np.asarray(codes, dtype=np.int64).reshape(-1)
    return np.asarray([strings[int(code)] if code >= 0 else None for code in flat], dtype=object).reshape(
        np.asarray(codes).shape
    )


def _axes(shape: tuple[int, int, int]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    months, rollouts, slots = shape
    return (
        np.broadcast_to(np.arange(months)[:, None, None], shape).ravel(),
        np.broadcast_to(np.arange(rollouts)[None, :, None], shape).ravel(),
        np.broadcast_to(np.arange(slots)[None, None, :], shape).ravel(),
    )


def cash_balances(run: Any) -> pl.DataFrame:
    plan, output = _parts(run)
    state = np.moveaxis(output.state.cash, -1, 1)[:, :, : plan.external_cash_slot]
    months, rollouts, slots = _axes(state.shape)
    return pl.DataFrame(
        {
            "rollout_index": rollouts,
            "month_index": months,
            "agent_id": _strings(plan, plan.cash_agent_codes[slots]),
            "account_id": _strings(plan, plan.cash_account_codes[slots]),
            "balance_quanta": state.reshape(-1),
        }
    )


def asset_lots(run: Any) -> pl.DataFrame:
    plan, output = _parts(run)
    state = np.moveaxis(output.state.lots, -1, 1)
    months, rollouts, slots = _axes(state.shape)
    basis = np.broadcast_to(output.state.lot_cost_basis.T[None, :, :], state.shape)
    purchase_month = np.broadcast_to(output.state.lot_purchase_month.T[None, :, :], state.shape)
    quantities = state.reshape(-1).astype(np.float64) / plan.lot_quantity_scale[slots].astype(np.float64)
    return pl.DataFrame(
        {
            "rollout_index": rollouts,
            "month_index": months,
            "lot_id": _strings(plan, plan.lot_id_codes[slots]),
            "agent_id": _strings(plan, plan.lot_agent_codes[slots]),
            "account_id": _strings(plan, plan.lot_account_codes[slots]),
            "asset_id": np.asarray(
                [plan.assets[int(code)].wire_id for code in plan.lot_asset_codes[slots]], dtype=object
            ),
            "purchase_month_index": purchase_month.reshape(-1),
            "cost_basis_per_unit_quanta": basis.reshape(-1),
            "remaining_quantity_quanta": state.reshape(-1),
            "quantity_scale": plan.lot_quantity_scale[slots],
            "remaining_quantity": quantities,
        }
    )


def ordinary_income_ytd(run: Any) -> pl.DataFrame:
    plan, output = _parts(run)
    state = np.moveaxis(output.state.ordinary, -1, 1)
    shape = state.shape
    months, rollouts, buckets = _axes(shape)
    profiles, sources = plan.tax.buckets.split_rows(buckets)
    source_ids = np.asarray(plan.tax.buckets.source_wire_ids())
    return pl.DataFrame(
        {
            "rollout_index": rollouts,
            "month_index": months,
            "agent_id": _strings(plan, plan.tax.profile_agent)[profiles],
            "income_source": source_ids[sources],
            "ordinary_income_quanta": state.reshape(-1),
        }
    )


def capital_gains_ytd(run: Any) -> pl.DataFrame:
    plan, output = _parts(run)
    state = np.moveaxis(output.state.capital_gain_ytd, -1, 1)
    active = np.moveaxis(output.state.capital_gain_active, -1, 1)
    h1, rollout_count, agent_count, _ = state.shape
    months = np.broadcast_to(np.arange(h1)[:, None, None, None], state.shape)
    rollouts = np.broadcast_to(np.arange(rollout_count)[None, :, None, None], state.shape)
    agents = np.broadcast_to(np.arange(agent_count)[None, None, :, None], state.shape)
    labels = np.broadcast_to(np.array(["ltcg", "stcg"], dtype=object)[None, None, None, :], state.shape)
    mask = active.reshape(-1)
    return pl.DataFrame(
        {
            "rollout_index": rollouts.reshape(-1)[mask],
            "month_index": months.reshape(-1)[mask],
            "agent_id": _strings(plan, plan.capital_gain_agent_codes)[agents.reshape(-1)[mask]],
            "classification": labels.reshape(-1)[mask],
            "gain_quanta": state.reshape(-1)[mask],
        }
    )


def tax_liabilities(run: Any) -> pl.DataFrame:
    plan, output = _parts(run)
    amounts = np.asarray(output.taxes.liability_amount)
    active = np.asarray(output.taxes.liability_active, dtype=bool)
    previous_amount = np.concatenate((np.zeros_like(amounts[:1]), amounts[:-1]), axis=0)
    previous_active = np.concatenate((np.zeros_like(active[:1]), active[:-1]), axis=0)
    changed = ((amounts != previous_amount) | (active != previous_active)).any(axis=2)
    months, slots, rollouts = np.argwhere(changed[:, :, None] & active).T
    # A liability the rollout never lived to be assessed does not exist, so it reports nothing
    # — not even a zero. The assessment lands the month after its tax year closes, and a
    # rollout that ran out of cash before then never reached it; this engine cannot leave a
    # vectorized scan early, so it keeps stepping under a mask and marks the liability active
    # anyway.
    #
    # This drops the whole liability, not the months after the failure: a liability that *was*
    # assessed stays on the books, and its later months are state the frozen rollout still has
    # (they read zero once it stops). Events are the opposite case and `codec/plan.py` states
    # that rule — a stopped rollout has no later events, because nothing later happens in it.
    assessed = plan.tax_liabilities.year_end_month.astype(np.int64)[slots] + 1
    reached = assessed <= _last_reported_month(plan, output)[rollouts]
    months, slots, rollouts = months[reached], slots[reached], rollouts[reached]
    profile = plan.tax_liabilities.profile_index.astype(np.int64)[slots]
    links = plan.tax_liabilities.link_index.astype(np.int64)[slots]
    return pl.DataFrame(
        {
            "rollout_index": rollouts,
            "month_index": months + 1,
            "agent_id": _strings(plan, plan.tax.profile_agent)[profile],
            "jurisdiction_id": _strings(plan, plan.tax.link_jurisdiction)[links],
            "tax_year_end_month": plan.tax_liabilities.year_end_month[slots],
            "amount_owed_quanta": amounts[months, slots, rollouts],
        }
    )


def property_state(run: Any) -> pl.DataFrame:
    plan, output = _parts(run)
    basis = np.moveaxis(output.state.property_basis, -1, 1)
    active = np.moveaxis(output.state.property_active, -1, 1)
    shape = basis.shape
    months, rollouts, props = _axes(shape)
    mask = active.reshape(-1)
    return pl.DataFrame(
        {
            "rollout_index": rollouts[mask],
            "month_index": months[mask],
            "property_id": _strings(plan, plan.properties.id)[props[mask]],
            "location_id": _strings(plan, plan.properties.location_id)[props[mask]],
            "purchase_month_index": plan.properties.month.astype(np.int64)[props[mask]],
            "adjusted_basis_quanta": basis.reshape(-1)[mask],
        }
    )


def property_stakes(run: Any) -> pl.DataFrame:
    plan, output = _parts(run)
    active = np.moveaxis(output.state.property_active, -1, 1)
    contribution = np.moveaxis(output.state.property_contribution, -1, 1)
    equity = np.moveaxis(output.state.property_equity, -1, 1)
    shape = active.shape
    months, rollouts, props = _axes(shape)
    mask = active.reshape(-1)
    return pl.DataFrame(
        {
            "rollout_index": rollouts[mask],
            "month_index": months[mask],
            "property_id": _strings(plan, plan.properties.id)[props[mask]],
            "agent_id": _strings(plan, plan.properties.buyer_agent)[props[mask]],
            "contribution_used_quanta": contribution.reshape(-1)[mask],
            "equity_ledger_quanta": equity.reshape(-1)[mask],
        }
    )


def liabilities(run: Any) -> pl.DataFrame:
    plan, output = _parts(run)
    principal = np.moveaxis(output.state.liability_principal, -1, 1)
    active = np.moveaxis(output.state.liability_active, -1, 1)
    shape = principal.shape
    months, rollouts, liabs = _axes(shape)
    mask = active.reshape(-1)
    liability = liabs[mask]
    property_slot = plan.liabilities.property_slot.astype(np.int64)
    property_codes = plan.properties.id[property_slot]
    return pl.DataFrame(
        {
            "rollout_index": rollouts[mask],
            "month_index": months[mask],
            "liability_id": _strings(plan, plan.liabilities.codes)[liability],
            "agent_id": _strings(plan, plan.liabilities.agent)[liability],
            "payment_account_id": _strings(plan, plan.liabilities.payment_account)[liability],
            "counterparty_agent_id": _strings(plan, plan.liabilities.counterparty_agent)[liability],
            "counterparty_account_id": _strings(plan, plan.liabilities.counterparty_account)[liability],
            "property_id": _strings(plan, property_codes)[liability],
            "principal_quanta": principal.reshape(-1)[mask],
            "annual_interest_rate": plan.liabilities.annual_rate[liability],
            "term_months": plan.liabilities.term_months[liability],
            "origination_month_index": plan.properties.month.astype(np.int64)[property_slot][liability],
            "monthly_payment_quanta": np.moveaxis(output.state.liability_monthly_payment, -1, 1).reshape(-1)[mask],
            "interest_paid_ytd_quanta": np.moveaxis(output.state.liability_interest_ytd, -1, 1).reshape(-1)[mask],
        }
    )


def rollout_status(run: Any) -> pl.DataFrame:
    plan, output = _parts(run)
    month = plan.horizon_months
    failed = np.asarray(output.state.failed[month], dtype=bool)
    failed_month = np.asarray(output.state.failed_month[month], dtype=np.int64)
    failed_month_values = [None if int(value) < 0 else int(value) for value in failed_month]
    return pl.DataFrame(
        {
            "rollout_index": np.arange(failed.size, dtype=np.int64),
            "status": np.where(failed, "failed_insufficient_cash", "active"),
            "failed_month": failed_month_values,
        }
    )


def series_values(run: Any) -> pl.DataFrame:
    rows = [
        frame.with_columns(pl.lit(key.wire_id, dtype=pl.Utf8).alias("series_id")).select(
            "rollout_index", "month_index", "series_id", "value"
        )
        for key, frame in run.external_series.levels.value_rows()
    ]
    return (
        pl.concat(rows)
        if rows
        else pl.DataFrame(
            schema={"rollout_index": pl.Int64, "month_index": pl.Int64, "series_id": pl.Utf8, "value": pl.Float64}
        )
    )
