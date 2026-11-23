"""Export all dashboard routes, including those available with SSO configured."""

import json

from props.backend.api import build_app

if __name__ == "__main__":
    print(json.dumps(build_app(lifespan=None, sso_enabled=True).openapi(), indent=2))
