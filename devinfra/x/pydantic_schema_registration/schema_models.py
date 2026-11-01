"""Models used to inspect Pydantic's multi-adapter JSON Schema allocator."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Json, create_model


class SharedRecord(BaseModel):
    label: str
    sequence: int


FirstSameName = create_model("SameName", first_value=(str, ...), __module__="pydantic_registration_spike.first")
SecondSameName = create_model("SameName", second_value=(int, ...), __module__="pydantic_registration_spike.second")

FirstRoot = create_model("FirstRoot", shared=(SharedRecord, ...), same_name=(FirstSameName, ...))
SecondRoot = create_model("SecondRoot", shared=(SharedRecord, ...), same_name=(SecondSameName, ...))


class JsonAndDecimal(BaseModel):
    parsed_json: Json[dict[str, int]]
    amount: Decimal
