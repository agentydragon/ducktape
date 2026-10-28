from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import FastAPI
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class InvoiceLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str
    quantity: int = Field(gt=0)
    unit_price: Decimal = Field(gt=0, max_digits=8, decimal_places=2)


class CardPayment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["card"]
    last_four: str = Field(pattern=r"^[0-9]{4}$")


class BankPayment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["bank"]
    account_reference: str


PaymentMethod = Annotated[CardPayment | BankPayment, Field(discriminator="kind")]


class HTTPTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str


class CreateInvoiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memo: str | None = None
    submitted_at: AwareDatetime
    payment: PaymentMethod
    lines: list[InvoiceLine]
    named_lines: dict[str, InvoiceLine]


class InvoiceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    total: Decimal = Field(max_digits=10, decimal_places=2)
    created_at: AwareDatetime
    lines: list[InvoiceLine]
    trace: HTTPTrace
    http_trace: HTTPTrace


app = FastAPI()


@app.post("/invoices", response_model=InvoiceResponse)
def create_invoice(request: CreateInvoiceRequest) -> InvoiceResponse:
    raise NotImplementedError(request)
