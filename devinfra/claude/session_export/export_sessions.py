"""Export Claude Code cloud-session event logs from claude.ai to a local archive.

Read-only against a private API. Authenticates with a dedicated OAuth grant (`pair` mints it) or, as the
browser does, a claude.ai `sessionKey` cookie. See README.md.

Usage:
    bb run //devinfra/claude/session_export:export_sessions_bin -- pair --credentials-file F
    bb run //devinfra/claude/session_export:export_sessions_bin -- export --credentials-file F --out DIR
    bb run //devinfra/claude/session_export:export_sessions_bin -- count --cookie-file F
    bb run //devinfra/claude/session_export:export_sessions_bin -- verify --out DIR
    SESSION_SYNC_DATABASE_URL=postgresql://... \\
        bb run //devinfra/claude/session_export:export_sessions_bin -- sync --credentials-file F
    bb run //devinfra/claude/session_export:export_sessions_bin -- serve   # settings from SESSION_SYNC_*
    bb run //devinfra/claude/session_export:export_sessions_bin -- probe --credentials-file F [--session ID]
"""

import argparse
import asyncio
import logging
import statistics
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import assert_never

import httpx
import uvicorn

from devinfra.claude.session_export.api import SessionCookie, SessionsApi
from devinfra.claude.session_export.archive import export_all, verify_archive
from devinfra.claude.session_export.database_migrate import RUNNER
from devinfra.claude.session_export.models import SessionSummary, canonical_id
from devinfra.claude.session_export.oauth import CALLBACK_PORT, DEFAULT_SCOPES, CredentialStore, OAuthTokenSource, pair
from devinfra.claude.session_export.probe import probe
from devinfra.claude.session_export.settings import (
    DEFAULT_SYNC_INTERVAL_SECONDS,
    DEFAULT_SYNC_WORKERS,
    ControlSettings,
    ServeSettings,
    SyncSettings,
    WebSettings,
)
from devinfra.claude.session_export.store import SessionStore, make_engine
from devinfra.claude.session_export.supervisor import SyncSupervisor
from devinfra.claude.session_export.sync import sync_once
from devinfra.claude.session_export.web import create_app, create_control_app, create_web_app
from util.bazel.workspace import get_build_working_directory

logger = logging.getLogger(__name__)


class Command(StrEnum):
    PAIR = "pair"
    EXPORT = "export"
    COUNT = "count"
    VERIFY = "verify"
    SYNC = "sync"
    SERVE = "serve"
    WEB = "web"
    CONTROL = "control"
    PROBE = "probe"


def announce(url: str) -> None:
    print(
        f"Open this URL in a browser signed in to the account and approve:\n\n{url}\n\nWaiting for the callback...",
        flush=True,
    )


async def count_events(api: SessionsApi, workers: int) -> None:
    sessions = [s async for s in api.list_sessions()]
    slots = asyncio.Semaphore(workers)

    async def newest(session: SessionSummary) -> int:
        async with slots:
            return await api.newest_sequence_num(canonical_id(session.id))

    async with asyncio.TaskGroup() as tasks:
        pending = [tasks.create_task(newest(s)) for s in sessions]
    counts = [t.result() for t in pending]
    logger.info(
        "sessions=%d events=%d median=%d max=%d",
        len(counts),
        sum(counts),
        statistics.median(counts or [0]),
        max(counts, default=0),
    )


@asynccontextmanager
async def open_api(args: argparse.Namespace) -> AsyncIterator[SessionsApi]:
    if args.cookie_file:
        cookie = SessionCookie.from_file(get_build_working_directory() / args.cookie_file)
        async with SessionsApi.for_cookie(cookie) as api:
            yield api
        return
    store = CredentialStore(get_build_working_directory() / args.credentials_file)
    async with (
        httpx.AsyncClient(timeout=30) as token_client,
        SessionsApi.for_oauth(OAuthTokenSource(store, token_client)) as api,
    ):
        yield api


async def run_sync(args: argparse.Namespace) -> None:
    database_url = SyncSettings().database_url
    await asyncio.to_thread(RUNNER.apply, database_url)
    engine = make_engine(database_url)
    store = SessionStore(engine)
    try:
        async with open_api(args) as api:
            while True:
                await sync_once(api, store, workers=args.workers)
                if args.once:
                    return
                await asyncio.sleep(args.interval)
    finally:
        await engine.dispose()


async def run_serve() -> None:
    """Local all-in-one mode: the sync and the page that pairs it share one credential-owning process."""
    settings = ServeSettings()
    await asyncio.to_thread(RUNNER.apply, settings.database_url)
    engine = make_engine(settings.database_url)
    try:
        async with httpx.AsyncClient(timeout=30) as token_client:
            supervisor = SyncSupervisor.for_settings(settings, store=SessionStore(engine), token_client=token_client)
            server = uvicorn.Server(
                uvicorn.Config(
                    create_app(supervisor=supervisor, settings=settings),
                    host=settings.host,
                    port=settings.port,
                    log_level="info",
                )
            )

            async def serve_then_stop_syncing(syncing: asyncio.Task[None]) -> None:
                await server.serve()
                syncing.cancel()

            # If the loop dies the process must too, so the pod restarts rather than serving a page over nothing.
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(serve_then_stop_syncing(tasks.create_task(supervisor.run())))
    finally:
        await engine.dispose()


async def run_web() -> None:
    """Serve the owner-authenticated page; control operations go to the private control service."""
    settings = WebSettings()
    await asyncio.to_thread(RUNNER.apply, settings.database_url)
    async with httpx.AsyncClient(base_url=settings.control_base_url, timeout=30) as control_client:
        server = uvicorn.Server(
            uvicorn.Config(
                create_web_app(settings=settings, control_client=control_client),
                host=settings.host,
                port=settings.port,
                log_level="info",
            )
        )
        await server.serve()


async def run_control() -> None:
    """Own the rotating Claude credential and sync loop behind the private control Service."""
    settings = ControlSettings()
    await asyncio.to_thread(RUNNER.apply, settings.database_url)
    engine = make_engine(settings.database_url)
    try:
        async with httpx.AsyncClient(timeout=30) as token_client:
            supervisor = SyncSupervisor.for_settings(settings, store=SessionStore(engine), token_client=token_client)
            server = uvicorn.Server(
                uvicorn.Config(
                    create_control_app(supervisor=supervisor), host=settings.host, port=settings.port, log_level="info"
                )
            )

            async def serve_then_stop_syncing(syncing: asyncio.Task[None]) -> None:
                await server.serve()
                syncing.cancel()

            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(serve_then_stop_syncing(tasks.create_task(supervisor.run())))
    finally:
        await engine.dispose()


async def run_pair(args: argparse.Namespace) -> None:
    store = CredentialStore(get_build_working_directory() / args.credentials_file)
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            async with asyncio.timeout(args.timeout):
                await pair(client, store, scopes=args.scope or DEFAULT_SCOPES, port=args.port, announce=announce)
        except TimeoutError as e:
            raise TimeoutError(f"no authorization callback on port {args.port} within {args.timeout:.0f}s") from e


async def async_main(command: Command, args: argparse.Namespace) -> None:
    match command:
        case Command.PAIR:
            await run_pair(args)
        case Command.EXPORT:
            async with open_api(args) as api:
                await export_all(
                    api,
                    get_build_working_directory() / args.out,
                    workers=args.workers,
                    session_ids=args.ids.split(",") if args.ids else None,
                    limit=args.limit_sessions,
                )
        case Command.COUNT:
            async with open_api(args) as api:
                await count_events(api, args.workers)
        case Command.VERIFY:
            verify_archive(get_build_working_directory() / args.out)
        case Command.SYNC:
            await run_sync(args)
        case Command.SERVE:
            await run_serve()
        case Command.WEB:
            await run_web()
        case Command.CONTROL:
            await run_control()
        case Command.PROBE:
            store = CredentialStore(get_build_working_directory() / args.credentials_file)
            await probe(store, session_id=args.session, listen_seconds=args.listen_seconds)
        case _:
            assert_never(command)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    pair_parser = commands.add_parser(Command.PAIR, help="mint a dedicated OAuth grant through the browser")
    pair_parser.add_argument("--credentials-file", required=True, help="where to write the credential (0600)")
    pair_parser.add_argument(
        "--scope",
        action="append",
        help=f"repeatable; default: {' '.join(DEFAULT_SCOPES)}. Choose the narrowest that works",
    )
    pair_parser.add_argument(
        "--port", type=int, default=CALLBACK_PORT, help="loopback port of the registered redirect URI"
    )
    pair_parser.add_argument("--timeout", type=float, default=300, help="seconds to wait for the browser callback")
    export = commands.add_parser(Command.EXPORT, help="download every session's events into --out (resumable)")
    export.add_argument("--out", required=True)
    export.add_argument("--ids", help="only these comma-separated session ids (`session_…` or `cse_…`)")
    export.add_argument("--limit-sessions", type=int, help="only the first N sessions of the list, newest first")
    count = commands.add_parser(Command.COUNT, help="print how many events the account holds; downloads nothing")
    verify = commands.add_parser(Command.VERIFY, help="re-read --out and check it; no network")
    verify.add_argument("--out", required=True)
    commands.add_parser(Command.SERVE, help="run the sync with its login-protected pairing page in one process")
    commands.add_parser(Command.WEB, help="serve the login-protected web/API tier, proxying control operations")
    commands.add_parser(Command.CONTROL, help="run the private sync owner and its control API")
    probe_parser = commands.add_parser(
        Command.PROBE, help="try the live routes under different headers and hosts, read-only; never refreshes"
    )
    probe_parser.add_argument(
        "--credentials-file", required=True, help="OAuth credential; only its access token is used"
    )
    probe_parser.add_argument("--session", help="also listen to this session's event stream (`session_…` or `cse_…`)")
    probe_parser.add_argument("--listen-seconds", type=float, default=10, help="how long to hold each stream open")
    sync = commands.add_parser(
        Command.SYNC, help="keep a Postgres database level with every session's events, until stopped"
    )
    sync.add_argument("--credentials-file", required=True, help="OAuth credential written by `pair`")
    sync.set_defaults(cookie_file=None)
    sync.add_argument("--workers", type=int, default=DEFAULT_SYNC_WORKERS, help="sessions fetched concurrently")
    sync.add_argument("--interval", type=float, default=DEFAULT_SYNC_INTERVAL_SECONDS, help="seconds between cycles")
    sync.add_argument("--once", action="store_true", help="run one cycle and exit")
    for network_command, default_workers in ((export, 3), (count, 4)):
        credential = network_command.add_mutually_exclusive_group(required=True)
        credential.add_argument("--credentials-file", help="OAuth credential written by `pair`")
        credential.add_argument("--cookie-file", help="file holding sessionKey= and lastActiveOrg=")
        network_command.add_argument(
            "--workers", type=int, default=default_workers, help="sessions fetched concurrently"
        )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # otherwise one line per 500-event page

    asyncio.run(async_main(Command(args.command), args))


if __name__ == "__main__":
    main()
