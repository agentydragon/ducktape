from __future__ import annotations

from functools import cached_property

from pydantic import BaseModel, ConfigDict


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, ignored_types=(cached_property,))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", ignored_types=(cached_property,))
