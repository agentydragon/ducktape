"""Build GEPA checkpoint from historical database evaluations for warm-start.

Critical Invariant: Deterministic Valset Ordering
==================================================

GEPA checkpoints store validation scores keyed by integer indices (0, 1, 2, ...), where
each index corresponds to a position in the valset list. When loading a checkpoint, we
MUST pass the exact same valset in the exact same order, otherwise scores point to wrong examples.

Enforcing Deterministic Order
------------------------------

Two-level ordering ensures stability:

1. **Snapshot level:** Query with `order_by(Snapshot.slug)`
2. **Example level:** Query with `order_by(Example.example_kind, Example.files_hash)`

PostgreSQL order is otherwise arbitrary.

Index Mapping Strategy
----------------------

Build reverse index from valset to enable historical run lookup:
  valset_idx_by_key[(snapshot_slug, scope_hash)] = list_index

Historical runs store (snapshot_slug, scope_hash) but not full Example objects.
We map these keys to current valset indices to populate the sparse score matrix.

See build_historical_gepa_state() implementation for complete warm-start logic.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy import text

from props.core.ids import SnapshotSlug
from props.db.examples import Example

logger = logging.getLogger(__name__)


def _compute_pareto_frontier_from_sql(
    session,
    critic_model: str,
    split: str,
    valset_idx_by_key: dict[tuple[SnapshotSlug, str], int],
    sha_to_prog_idx: dict[str, int],
) -> tuple[dict[int, float], dict[int, set[int]]]:
    """Query Pareto frontier from pareto_frontier_by_example view.

    Args:
        session: Database session
        critic_model: Model name to filter
        split: Split to filter (e.g., "valid")
        valset_idx_by_key: Maps (snapshot_slug, scope_hash) to validation dataset index
        sha_to_prog_idx: Maps prompt SHA to program index in checkpoint

    Returns:
        (pareto_front_valset, program_at_pareto_front_valset) tuple where:
        - pareto_front_valset: Maps val_idx -> best_score
        - program_at_pareto_front_valset: Maps val_idx -> set of program indices achieving best score
    """
    pareto_data = session.execute(
        text("""
            SELECT
                snapshot_slug,
                scope_hash,
                best_recall,
                winning_prompt_shas
            FROM pareto_frontier_by_example
            WHERE split = :split AND critic_model = :critic_model
        """),
        {"split": split, "critic_model": critic_model},
    ).fetchall()

    pareto_front_valset: dict[int, float] = {}
    program_at_pareto_front_valset: dict[int, set[int]] = defaultdict(set)

    for snapshot_slug, scope_hash, best_recall, winning_prompt_shas in pareto_data:
        # Map (snapshot_slug, scope_hash) to valset index
        val_idx = valset_idx_by_key.get((snapshot_slug, scope_hash))
        if val_idx is None:
            # Example not in current valset (e.g., split changed)
            continue

        pareto_front_valset[val_idx] = best_recall

        # Map winning prompt SHAs to program indices
        # All winning prompts must be in historical set (integrity check)
        program_at_pareto_front_valset[val_idx] = {sha_to_prog_idx[sha] for sha in winning_prompt_shas}

    return pareto_front_valset, program_at_pareto_front_valset


def build_historical_gepa_state(valset: list[Example], critic_model: str, grader_model: str) -> dict | None:
    """Build GEPAState dict from historical critic+grader runs in database.

    Reconstructs:
    - All unique prompts as program_candidates
    - Validation scores for each prompt across validation set
    - Pareto frontier computed from historical data

    CRITICAL: Index Mapping
    ------------------------
    GEPA stores validation scores keyed by integer indices (DataIds), which are
    implicit list positions: valset[0] → DataId 0, valset[1] → DataId 1, etc.

    Historical runs in the database store (snapshot_slug, scope_hash) but not
    the full Example objects. We must map these to current valset indices:

        1. Build index: (slug, scope_hash) → valset_idx
        2. Match historical runs via their (snapshot_slug, scope_hash)
        3. Store scores keyed by valset_idx: {0: 0.85, 2: 0.90, ...}

    This requires valset to have deterministic ordering (see load_datasets()).

    Args:
        valset: Validation dataset (list of Example objects) - MUST be in stable order
        critic_model: Model name to filter critic runs
        grader_model: Unused (kept for API compatibility). Recall scores are aggregated
                      across all grading runs regardless of grader model.

    Returns:
        Dict suitable for pickle.dump as gepa_state.bin, or None if no historical data

    Note:
        Sets total_num_evals=0 so budget applies to this run only,
        not counting historical evaluations.
    """
    # TODO(scope_hash migration): Warm-start is temporarily disabled during migration
    # Database views still use scope_hash but Example ORM uses (scope_kind, trigger_set_id)
    # Return None to indicate no warm-start data (GEPA will start from empty state)
    logger.warning(
        "GEPA warm-start temporarily disabled during scope_hash migration. "
        "Database views use scope_hash but Example ORM uses (scope_kind, trigger_set_id). "
        "Starting from empty state."
    )
    return None
