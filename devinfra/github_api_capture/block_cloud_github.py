"""Temporary, explicitly enabled mitigation for cloud GitHub polling fan-out."""

import asyncio
import json
import time
from pathlib import Path

from mitmproxy import ctx, http
from mitmproxy.addonmanager import Loader


class BlockCloudGithub:
    def __init__(self) -> None:
        self.blocked_requests = 0
        self.started_at = time.time()
        self.heartbeat: asyncio.Task[None] | None = None

    def load(self, loader: Loader) -> None:
        loader.add_option("block_cloud_github_batch", bool, False, "Temporarily block cloud GitHub batch polling.")
        loader.add_option("cloud_github_block_events", str, "", "Private append-only metadata/heartbeat log.")

    def record(self, event: str) -> None:
        record = {
            "event": event,
            "at": time.time(),
            "started_at": self.started_at,
            "enabled": ctx.options.block_cloud_github_batch,
            "blocked_requests": self.blocked_requests,
        }
        line = json.dumps(record)
        # No request headers, paths, identifiers, variables, or payloads are logged.
        if ctx.options.cloud_github_block_events:
            with Path(ctx.options.cloud_github_block_events).open("a") as output:
                output.write(line + "\n")
        print(line, flush=True)

    async def heartbeats(self) -> None:
        while True:
            await asyncio.sleep(30)
            self.record("heartbeat")

    def running(self) -> None:
        self.record("started")
        self.heartbeat = asyncio.create_task(self.heartbeats())

    def request(self, flow: http.HTTPFlow) -> None:
        if (
            not ctx.options.block_cloud_github_batch
            or flow.request.method != "POST"
            or flow.request.host != "claude.ai"
            or flow.request.path.split("?", 1)[0] != "/v1/code/github/batch-branch-status"
        ):
            return
        flow.response = http.Response.make(
            429,
            json.dumps(
                {"error": {"type": "rate_limit_error", "message": "Cloud GitHub polling temporarily blocked locally"}}
            ),
            {"content-type": "application/json", "retry-after": "3600"},
        )
        self.blocked_requests += 1
        self.record("blocked")

    def done(self) -> None:
        if self.heartbeat is not None:
            self.heartbeat.cancel()
        self.record("stopped")


addons = [BlockCloudGithub()]
