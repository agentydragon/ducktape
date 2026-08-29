"""Shared FastAPI dependencies for the grants router family."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request

from haku.console.grants.catalog import GrantCatalog


def grant_catalog(request: Request) -> GrantCatalog:
    return cast(GrantCatalog, request.app.state.grant_catalog)


GrantCatalogDep = Annotated[GrantCatalog, Depends(grant_catalog)]
