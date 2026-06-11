"""Export OpenAPI schema from FastAPI app to stdout."""

import json

from props.backend.app import create_app

if __name__ == "__main__":
    app = create_app()
    schema = app.openapi()
    print(json.dumps(schema, indent=2))
