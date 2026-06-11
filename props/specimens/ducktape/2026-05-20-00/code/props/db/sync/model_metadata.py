"""Sync model metadata from static sources to database."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from openai_utils.model_metadata import MODEL_METADATA
from props.db.models import ModelMetadata
from props.db.sync.stats import SyncStats

if TYPE_CHECKING:
    from props.config import PropsConfig

logger = logging.getLogger(__name__)


def sync_model_metadata_with_session(session: Session, config: PropsConfig | None = None) -> SyncStats:
    """Sync model_metadata table from MODEL_METADATA and config sources.

    Syncs from two sources:
    - OpenAI models from openai_utils.model_metadata.MODEL_METADATA (upstream_name=NULL)
    - Custom models from PropsConfig.models (upstream_name/upstream_model from config)
    """
    # Build complete source model set
    source_models: dict[str, ModelMetadata] = {}

    # Add OpenAI models (no upstream fields = defaults)
    for model_id, meta in MODEL_METADATA.items():
        source_models[model_id] = ModelMetadata(
            model_id=model_id,
            input_usd_per_1m_tokens=meta.input_usd_per_1m_tokens,
            cached_input_usd_per_1m_tokens=meta.cached_input_usd_per_1m_tokens,
            output_usd_per_1m_tokens=meta.output_usd_per_1m_tokens,
            context_window_tokens=meta.context_window_tokens,
            max_output_tokens=meta.max_output_tokens,
            upstream_name=None,
            upstream_model=None,
        )

    # Add custom models from config
    if config is not None:
        for custom in config.models:
            source_models[custom.name] = ModelMetadata(
                model_id=custom.name,
                input_usd_per_1m_tokens=custom.input_usd_per_1m_tokens,
                cached_input_usd_per_1m_tokens=custom.cached_input_usd_per_1m_tokens,
                output_usd_per_1m_tokens=custom.output_usd_per_1m_tokens,
                context_window_tokens=custom.context_window_tokens,
                max_output_tokens=custom.max_output_tokens,
                upstream_name=custom.upstream,
                upstream_model=custom.upstream_model,
            )

    # Full sync: make DB exactly match source
    logger.info(f"Syncing model_metadata table (source: {len(source_models)} models)...")

    db_models = {m.model_id: m for m in session.query(ModelMetadata).all()}
    source_model_ids = set(source_models.keys())
    db_model_ids = set(db_models.keys())

    added = 0
    updated = 0
    deleted = 0

    # Delete orphaned models (in DB but not in source)
    for model_id in db_model_ids - source_model_ids:
        logger.info(f"  Deleting orphaned model: {model_id}")
        session.delete(db_models[model_id])
        deleted += 1

    # Add/update from source using merge (handles both cases)
    for model_id, model_meta in source_models.items():
        is_new = model_id not in db_model_ids
        session.merge(model_meta)
        if is_new:
            logger.debug(f"  Adding model: {model_id}")
            added += 1
        else:
            # Note: merge() updates if changed; count all as updated for stats
            updated += 1

    session.flush()

    logger.info(
        f"Model metadata synced: +{added} added, ~{updated} updated, -{deleted} deleted, ={len(source_models)} total"
    )
    return SyncStats(added=added, updated=updated, deleted=deleted, total=len(source_models))
