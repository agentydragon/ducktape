"""Model metadata API route — lists known models with pricing and limits."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from props.backend.deps import AdminDb
from props.db.models import ModelMetadata

router = APIRouter()


class ModelMetadataInfo(BaseModel):
    model_id: str
    input_usd_per_1m_tokens: float
    cached_input_usd_per_1m_tokens: float
    output_usd_per_1m_tokens: float
    context_window_tokens: int
    max_output_tokens: int


class ModelMetadataResponse(BaseModel):
    models: list[ModelMetadataInfo]


@router.get("")
def list_model_metadata(admin_db: AdminDb) -> ModelMetadataResponse:
    """List all known models with pricing and context limits."""
    with admin_db.session() as session:
        rows = session.query(ModelMetadata).order_by(ModelMetadata.model_id).all()
        return ModelMetadataResponse(
            models=[
                ModelMetadataInfo(
                    model_id=row.model_id,
                    input_usd_per_1m_tokens=row.input_usd_per_1m_tokens,
                    cached_input_usd_per_1m_tokens=row.cached_input_usd_per_1m_tokens,
                    output_usd_per_1m_tokens=row.output_usd_per_1m_tokens,
                    context_window_tokens=row.context_window_tokens,
                    max_output_tokens=row.max_output_tokens,
                )
                for row in rows
            ]
        )
