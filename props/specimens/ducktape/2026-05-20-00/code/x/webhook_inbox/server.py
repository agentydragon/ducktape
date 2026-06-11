"""Uvicorn server entry point for webhook_inbox."""

import uvicorn

from x.webhook_inbox.webhook_inbox import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
