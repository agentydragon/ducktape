from __future__ import annotations

from datetime import date
from decimal import Decimal

from finance.augur.api.casing import plain_json
from finance.augur.api.schemas import ApiModel


class _NestedPayload(ApiModel):
    label: str
    missing: str | None = None


class _Payload(ApiModel):
    as_of: date
    amount: Decimal
    missing: str | None = None
    nested: tuple[_NestedPayload, ...]


def test_plain_json_recursively_serializes_pydantic_values_and_null_policy() -> None:
    payload = _Payload(as_of=date(2026, 8, 20), amount=Decimal("12.34"), nested=(_NestedPayload(label="kept"),))

    assert plain_json({"payload": payload, "items": (payload,)}) == {
        "payload": {"as_of": "2026-08-20", "amount": "12.34", "nested": [{"label": "kept"}]},
        "items": [{"as_of": "2026-08-20", "amount": "12.34", "nested": [{"label": "kept"}]}],
    }
    assert plain_json(payload, exclude_none=False) == {
        "as_of": "2026-08-20",
        "amount": "12.34",
        "missing": None,
        "nested": [{"label": "kept", "missing": None}],
    }
