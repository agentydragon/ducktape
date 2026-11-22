"""Emit the Augur API OpenAPI schema (to stdout) for frontend Zod/TS codegen.

Registers the real route graph (`build_app`) with no deployment bound and dumps its `.openapi()`,
adding the request schema for the undocumented CSV endpoint from its existing Pydantic model. The
document is a function of route signatures (request/response models) alone, so no catalog, model
or price client is acquired, and there is no separate schema-only app to drift.
"""

from __future__ import annotations

import json

from finance.augur.api.server import build_app
from finance.budget.wire import BudgetSummaryCsvRequest
from util.openapi_schema import register_openapi_model


def main() -> None:
    document = build_app(lifespan=None).openapi()
    # The CSV route stays out of path docs; its existing request model still drives frontend types.
    register_openapi_model(document, BudgetSummaryCsvRequest)
    print(json.dumps(document, indent=2))


if __name__ == "__main__":
    main()
