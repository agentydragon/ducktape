"""Uvicorn server entry point for gatelet."""

import uvicorn

from hack.gatelet.server.app import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
