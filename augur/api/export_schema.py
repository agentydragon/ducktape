"""Emit the Augur API OpenAPI schema (to stdout) for frontend Zod/TS codegen.

Builds the *real* FastAPI app from the light public fixture config and dumps its
`.openapi()`. The fixture (`augur/api/testdata/config.yaml`) constructs the full app and
registers every route — including `/api/calibration/*` — without touching the network, a
live Manifold, real model artifacts, or JS assets, so the emitted document carries every
component schema the frontend consumes. There is no separate schema-only app to drift.
"""

from __future__ import annotations

import json

from augur.api.config import load_augur_config
from augur.api.server import create_app_from_augur_config
from util.bazel.runfiles import get_required_own_repo_path


def main() -> None:
    config = load_augur_config(get_required_own_repo_path("augur/api/testdata/config.yaml"))
    print(json.dumps(create_app_from_augur_config(config).openapi(), indent=2))


if __name__ == "__main__":
    main()
