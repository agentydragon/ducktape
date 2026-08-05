"""Obligation domain decoders: accruals, settlements, derived transfers, failures.
The compile-side twin is `ObligationCompileOutput` + `_compile_obligations` in
`augur.sim.compiler`."""

from __future__ import annotations

import numpy as np
import polars as pl

from finance.augur.sim.buffers import SimulationBuffers
from finance.augur.sim.codec.helpers import code_column, codes_to_strings, frame_from_columns, usd_column
from finance.augur.sim.compiler import CompiledSimulation
from finance.augur.sim.events import EVENT_FRAMES


def decode_obligations(
    plan: CompiledSimulation, buffers: SimulationBuffers
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Returns (accruals, settlements, derived_transfers, failures).

    Base mask is `obligation_active`; the transfer-row subset gates on
    `obligation_paid > 0`, the failure-row subset on `obligation_failure_active`.
    """

    active = buffers.obligations.active  # (M, S, R)
    if active.any():
        months, slots, rollouts = np.argwhere(active).T
    else:
        months = slots = rollouts = np.array([], dtype=np.int64)
    # Per-event integer code arrays (lifted to strings lazily via CodeColumn dict-gather).
    cause_codes = plan.obligations.cause[months, slots]
    obligation_id_codes = plan.obligations.id[months, slots]
    type_codes = plan.obligations.type[months, slots]
    agent_codes = plan.obligations.agent[months, slots]
    from_account_codes = plan.obligations.from_account[months, slots]
    to_agent_codes = plan.obligations.to_agent[months, slots]
    to_account_codes = plan.obligations.to_account[months, slots]
    amount_due_cents = buffers.obligations.due[months, slots, rollouts]
    amount_paid_cents = buffers.obligations.paid[months, slots, rollouts]
    shortfall_cents = buffers.obligations.shortfall[months, slots, rollouts]
    amount_due = usd_column(amount_due_cents)
    amount_paid = usd_column(amount_paid_cents)
    shortfall = usd_column(shortfall_cents)
    attempt_policy = buffers.obligations.attempt_policy[months, slots, rollouts]
    attempted_sources_per_event = attempted_sources_for_policy_indices(plan, attempt_policy)

    accruals = frame_from_columns(
        EVENT_FRAMES.obligation_accruals,
        rollout_index=rollouts,
        month_index=months,
        cause_id=code_column(plan, cause_codes),
        obligation_id=code_column(plan, obligation_id_codes),
        obligation_type=code_column(plan, type_codes),
        agent_id=code_column(plan, agent_codes),
        from_account_id=code_column(plan, from_account_codes),
        to_agent_id=code_column(plan, to_agent_codes),
        to_account_id=code_column(plan, to_account_codes),
        amount_due_usd=amount_due,
    )
    settlements = frame_from_columns(
        EVENT_FRAMES.obligation_settlements,
        rollout_index=rollouts,
        month_index=months,
        cause_id=code_column(plan, cause_codes),
        obligation_id=code_column(plan, obligation_id_codes),
        obligation_type=code_column(plan, type_codes),
        agent_id=code_column(plan, agent_codes),
        from_account_id=code_column(plan, from_account_codes),
        amount_due_usd=amount_due,
        amount_paid_usd=amount_paid,
        shortfall_usd=shortfall,
        attempted_funding_sources=attempted_sources_per_event,
    )
    # Subset 1: obligations with paid > 0 emit a derived transfer row.
    paid_mask = amount_paid_cents > 0
    if paid_mask.any():
        transfers = frame_from_columns(
            EVENT_FRAMES.transfers,
            rollout_index=rollouts[paid_mask],
            month_index=months[paid_mask],
            cause_id=code_column(plan, cause_codes[paid_mask]),
            from_agent_id=code_column(plan, agent_codes[paid_mask]),
            from_account_id=code_column(plan, from_account_codes[paid_mask]),
            to_agent_id=code_column(plan, to_agent_codes[paid_mask]),
            to_account_id=code_column(plan, to_account_codes[paid_mask]),
            amount_usd=amount_paid[paid_mask],
            income_category=np.full(int(paid_mask.sum()), None, dtype=object),
        )
    else:
        transfers = EVENT_FRAMES.transfers.empty()
    # Subset 2: obligations whose failure flag fired emit a failure row.
    failure_mask = buffers.obligations.failure_active[months, slots, rollouts]
    if failure_mask.any():
        failed_obligation_ids = codes_to_strings(plan, obligation_id_codes[failure_mask])
        failure_cause_ids = np.array([f"{oid}_failure" for oid in failed_obligation_ids], dtype=object)
        failures = frame_from_columns(
            EVENT_FRAMES.rollout_failures,
            rollout_index=rollouts[failure_mask],
            month_index=months[failure_mask],
            cause_id=failure_cause_ids,
            agent_id=code_column(plan, agent_codes[failure_mask]),
            deficit_usd=shortfall[failure_mask],
            obligation_id=code_column(plan, obligation_id_codes[failure_mask]),
            obligation_type=code_column(plan, type_codes[failure_mask]),
            amount_due_usd=amount_due[failure_mask],
            amount_paid_usd=amount_paid[failure_mask],
            shortfall_usd=shortfall[failure_mask],
            attempted_funding_sources=attempted_sources_per_event[failure_mask],
        )
    else:
        failures = EVENT_FRAMES.rollout_failures.empty()
    return accruals, settlements, transfers, failures


def attempted_sources_for_policy_indices(plan: CompiledSimulation, attempt_policy: np.ndarray) -> np.ndarray:
    """Map a per-event `attempt_policy` int array to the matching joined-asset-names strings.

    `-1` (no attempting policy) maps to `""`. The result is an object-dtype array of strings,
    shape matching the input.
    """

    policy_count = plan.target_allocation_policies.sleeve_assets.shape[0]
    lookup = np.empty(policy_count + 1, dtype=object)
    lookup[0] = ""
    for policy in range(policy_count):
        lookup[policy + 1] = _attempted_sources(plan, policy)
    # Shift attempt_policy by +1 so -1 -> 0 (empty string).
    return lookup[attempt_policy.astype(np.int64) + 1]


def _attempted_sources(plan: CompiledSimulation, policy: int) -> str:
    """Per-policy joined-asset-names string used in obligation settlement / failure rows.

    Called once per policy at decode time (small fixed count) to populate the lookup table
    `attempted_sources_for_policy_indices` uses to gather per-event strings."""

    if policy < 0:
        return ""
    # `sleeve_assets` are AssetTable codes; lift to wire ids for the joined string. Padded
    # sleeves carry a negative code and name nothing, so they drop out.
    return ",".join(
        plan.assets[asset_code].wire_id
        for asset_code in plan.target_allocation_policies.sleeve_assets[policy].tolist()
        if asset_code >= 0
    )
