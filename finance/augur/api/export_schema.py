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


def main() -> None:
    document = build_app(lifespan=None).openapi()
    # The CSV route stays out of path docs; its existing request model still drives frontend types.
    csv_request_schema = BudgetSummaryCsvRequest.model_json_schema(ref_template="#/components/schemas/{model}")
    schemas = document["components"]["schemas"]
    for name, schema in csv_request_schema.pop("$defs", {}).items():
        schemas.setdefault(name, schema)
    schemas["BudgetSummaryCsvRequest"] = csv_request_schema
    print(json.dumps(document, indent=2))


if __name__ == "__main__":
    main()
