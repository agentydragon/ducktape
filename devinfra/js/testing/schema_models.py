from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict


class CreateWidgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


class Widget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str


app = FastAPI()


@app.post("/widgets", response_model=Widget)
def create_widget(request: CreateWidgetRequest) -> Widget:
    raise NotImplementedError(request)
