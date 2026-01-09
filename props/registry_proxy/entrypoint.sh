#!/bin/bash
set -euo pipefail

# Registry proxy entrypoint: runs FastAPI server with uvicorn
exec python -m uvicorn props.registry_proxy.proxy:app \
  --host 0.0.0.0 \
  --port 5051 \
  --log-level info
