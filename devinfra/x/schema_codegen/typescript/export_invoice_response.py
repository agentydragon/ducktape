from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from devinfra.x.schema_codegen.typescript.schema_models import HTTPTrace, InvoiceLine, InvoiceResponse


def main() -> None:
    response = InvoiceResponse(
        id=UUID("6245fd1e-0dd3-4f2e-93ab-fb27953fc2d4"),
        total=Decimal("39.90"),
        created_at=datetime.fromisoformat("2026-10-27T09:45:00-07:00"),
        lines=[InvoiceLine(sku="lamp", quantity=2, unit_price=Decimal("19.95"))],
        trace=HTTPTrace(request_id="req-http"),
        http_trace=HTTPTrace(request_id="req-title"),
    )
    print(response.model_dump_json())


if __name__ == "__main__":
    main()
