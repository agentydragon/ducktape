"""Shared FastAPI dependencies for rspcache proxy and admin apps."""

from typing import Annotated

from fastapi import Depends

from x.rspcache.responses_db import ResponsesDB

_db = ResponsesDB()


def get_db() -> ResponsesDB:
    return _db


Db = Annotated[ResponsesDB, Depends(get_db)]
