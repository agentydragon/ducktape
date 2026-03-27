#!/usr/bin/env python3
"""Demonstration of bootstrapping Ember's Matrix client."""

from __future__ import annotations

import asyncio
import os
from typing import Final

from ember.matrix_client import MatrixClient

# Configure the room ID via environment variable to avoid hard-coding it here.
ROOM_ID_ENV: Final[str] = "EMBER_MATRIX_ROOM_ID"


async def main() -> None:
    room_id = os.environ.get(ROOM_ID_ENV)
    if not room_id:
        raise SystemExit(f"Set {ROOM_ID_ENV} to the Matrix room (e.g. !abc123:example.org)")

    client = MatrixClient.from_projected_secrets()
    async with client.session() as matrix:
        await matrix.send_text_message(room_id, "Hello from Ember's matrix-client quickstart!")
        print(f"Sent message to {room_id}")

        try:
            async with asyncio.timeout(5.0):
                events = await matrix.get_events()
        except TimeoutError:
            events = []
        if events:
            print("Recent events:")
            for event in events:
                body = event.body if isinstance(event.body, str) else str(event.body)
                print(f"{event.sender}: {body}")
        else:
            print("No new events within the last 5 seconds.")


if __name__ == "__main__":
    asyncio.run(main())
