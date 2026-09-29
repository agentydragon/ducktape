"""Export Claude Code cloud-session event logs from claude.ai to a local archive.

Read-only against a private API, authenticated with your claude.ai `sessionKey` cookie. See README.md.

Usage:
    bb run //devinfra/claude/session_export:export_sessions_bin -- export --cookie-file F --out DIR
    bb run //devinfra/claude/session_export:export_sessions_bin -- count --cookie-file F
    bb run //devinfra/claude/session_export:export_sessions_bin -- verify --out DIR
"""

import argparse
import asyncio
import logging
import statistics
from enum import StrEnum

from devinfra.claude.session_export.api import SessionCookie, SessionsApi
from devinfra.claude.session_export.archive import canonical_id, export_all, verify_archive
from devinfra.claude.session_export.models import SessionSummary
from util.bazel.workspace import get_build_working_directory

logger = logging.getLogger(__name__)


class Command(StrEnum):
    EXPORT = "export"
    COUNT = "count"
    VERIFY = "verify"


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


async def async_main(args: argparse.Namespace) -> None:
    cookie = SessionCookie.from_file(get_build_working_directory() / args.cookie_file)
    async with SessionsApi(cookie) as api:
        if Command(args.command) is Command.EXPORT:
            await export_all(
                api,
                get_build_working_directory() / args.out,
                workers=args.workers,
                session_ids=args.ids.split(",") if args.ids else None,
                limit=args.limit_sessions,
            )
        else:
            await count_events(api, args.workers)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser(Command.EXPORT, help="download every session's events into --out (resumable)")
    export.add_argument("--out", required=True)
    export.add_argument("--ids", help="only these comma-separated session ids (`session_…` or `cse_…`)")
    export.add_argument("--limit-sessions", type=int, help="only the first N sessions of the list, newest first")
    count = commands.add_parser(Command.COUNT, help="print how many events the account holds; downloads nothing")
    verify = commands.add_parser(Command.VERIFY, help="re-read --out and check it; no network")
    verify.add_argument("--out", required=True)
    for network_command, default_workers in ((export, 3), (count, 4)):
        network_command.add_argument("--cookie-file", required=True, help="file holding sessionKey= and lastActiveOrg=")
        network_command.add_argument(
            "--workers", type=int, default=default_workers, help="sessions fetched concurrently"
        )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # otherwise one line per 500-event page

    if Command(args.command) is Command.VERIFY:
        verify_archive(get_build_working_directory() / args.out)
    else:
        asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
