"""The console side of one runner session: dial, handshake, roll replay, reconnect.

Harness-invariant. `Communicator` owns the WebSocket client and the two handshakes the console
speaks — the versions/launch negotiation (`Hello` then `HarnessLaunch`) and the neutral-operation
journal handshake (`RunnerHello` then `ConsoleResume`) — the tenacity reconnect that waits out a
rolling console, and the roll replay that hands a re-adopting console the frames and journal
batches it is missing. The run-loop drives it connection by connection and owns the CLI process
those connections serve; what it pumps through each connection is the <session_api.py> `SessionApi`.
"""

from __future__ import annotations

import logging

from tenacity import AsyncRetrying, before_sleep_log, retry_if_exception, stop_after_delay, wait_exponential
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidHandshake, InvalidStatus

from haku.runner.neutral_operations import GENERATION, ConsoleResume, RunnerHello
from haku.runner.protocol import CONSOLE_TO_RUNNER, ConsoleJournal, HarnessLaunch, Hello, RunnerJournal, TextWebSocket
from haku.runner.session_api import SessionApi

logger = logging.getLogger(__name__)

RECONNECT_BASE_DELAY = 1.0
RECONNECT_MAX_DELAY = 20.0
# A sandbox held for a console that never returns is worse than the wedged room it was protecting.
MAX_DISCONNECTED_SECONDS = 900.0


class ConsoleRefusedError(RuntimeError):
    """The console refused this runner for good — a generation mismatch, a consumed credential, a
    session already over. No redial can change it, so the sandbox exits and releases its claim."""


class ClientWebSocketAdapter(TextWebSocket):
    """Adapt websockets' client connection to the transport's text-only surface."""

    def __init__(self, connection: ClientConnection):
        self._connection = connection

    async def send_text(self, data: str) -> None:
        await self._connection.send(data)

    async def receive_text(self) -> str:
        data = await self._connection.recv()
        if not isinstance(data, str):
            raise ValueError("the runner protocol requires text WebSocket frames")
        return data

    async def close(self) -> None:
        await self._connection.close()


def _worth_redialling(error: BaseException) -> bool:
    """Whether a failed dial is a console that is not there *yet*, rather than one refusing us.

    See `NOT_ADMITTED_CODE` for why a refusal arrives as a 4xx handshake response instead of a close
    code. A 5xx is the Gateway with no ready backend — a console roll, from in here — and an
    `OSError` is the connection itself failing. Separate arms because `InvalidStatus` is not an
    `OSError`, so a 503 mid-roll would otherwise escape the loop and take the sandbox with it.

    **Do not tighten the 5xx arm to a status list.** A console whose session is still leased by a
    replica shutting down answers 503 deliberately, through the ASGI denial-response extension,
    precisely so this returns True — see `RunnerConnectionAuthentication.HELD`.
    """
    if isinstance(error, InvalidStatus):
        return error.response.status_code >= 500
    return isinstance(error, OSError | InvalidHandshake)


async def _receive_launch(websocket: TextWebSocket) -> HarnessLaunch:
    """Say which versions this image speaks, then read the launch the console chose.

    The hello goes first on **every** connection, not only the first: a console adopting a session
    after a roll is a different process and has to be told the same thing.
    """
    await websocket.send_text(Hello().model_dump_json())
    if not isinstance(launch := CONSOLE_TO_RUNNER.validate_json(await websocket.receive_text()), HarnessLaunch):
        raise ValueError(f"first runner protocol frame must be a launch, got {type(launch).__name__}")
    return launch


async def _handshake_journal(websocket: TextWebSocket) -> ConsoleResume:
    """Offer this image's generation and versions; read the console's resume — on every
    connection, because the durable batch cursor is exactly what a reconnect needs.

    A generation the console did not echo back is a console this runner must not serve: the
    maintenance-gated cut promises no old runner and new console (or the reverse) ever share a
    conversation, and this refusal is the runner's half of that promise.
    """
    await websocket.send_text(RunnerJournal(message=RunnerHello()).model_dump_json())
    match CONSOLE_TO_RUNNER.validate_json(await websocket.receive_text()):
        case ConsoleJournal(message=ConsoleResume() as resume):
            if resume.generation != GENERATION:
                raise ConsoleRefusedError(
                    f"transport generation mismatch: console={resume.generation!r} runner={GENERATION!r}"
                )
            return resume
        case other:
            raise ValueError(f"console answered the journal hello with {other.kind}")


class Communicator:
    """The console-facing transport of one runner session, harness-invariant."""

    def __init__(self, websocket_url: str, session_token: str | None):
        self._websocket_url = websocket_url
        self._headers: dict[str, str] | None = {"Authorization": f"Bearer {session_token}"} if session_token else None

    async def dial(self) -> TextWebSocket:
        """Connect, waiting out a console that is missing for as long as that is worth doing.

        The clock starts at each call, so the budget is "how long since this runner last had a
        console" rather than how long the session has run: any number of rolls is survivable, one
        unending outage is not. `OSError`/`InvalidHandshake` after the budget is spent is the
        console gone for good, which the caller reads as "release this sandbox".
        """

        async def dial_once() -> ClientConnection:
            return await connect(self._websocket_url, additional_headers=self._headers)

        connection: ClientConnection = await AsyncRetrying(
            retry=retry_if_exception(_worth_redialling),
            wait=wait_exponential(multiplier=RECONNECT_BASE_DELAY, max=RECONNECT_MAX_DELAY),
            stop=stop_after_delay(MAX_DISCONNECTED_SECONDS),
            before_sleep=before_sleep_log(logger, logging.INFO),
            reraise=True,
        )(dial_once)
        return ClientWebSocketAdapter(connection)

    async def handshake(self, websocket: TextWebSocket, session: SessionApi) -> HarnessLaunch:
        """Both handshakes on one connection, then the roll replay; returns the launch.

        A later connection brings a freshly built `start` frame whose process fields the caller
        **ignores** — argv, system prompt and MCP wiring belong to a process already running. What
        each connection genuinely brings is the two resume cursors — the console's frame cursor on
        `start`, its journal cursor on the `ConsoleResume` — and the replay each narrows.
        """
        launch = await _receive_launch(websocket)
        # Before the bootstrap, not with the replay: narration is numbered too, and a console that
        # already holds frames must not be sent one below its cursor.
        session.seed(launch.resume_from)
        resume = await _handshake_journal(websocket)
        # Replays go before live traffic on this socket. Duplicates with what the buffer still
        # holds are expected and dropped by the console — frames by runner position, batches by
        # idempotent commit.
        for text in session.missed(launch.resume_from):
            await websocket.send_text(text)
        for text in session.resumed(resume):
            await websocket.send_text(text)
        return launch
