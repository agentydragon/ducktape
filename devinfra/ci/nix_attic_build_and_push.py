"""Stream completed nix-fast-build outputs to one Attic client per cache job.

nix-fast-build 1.6.0 emits one BUILD JSON line after each finished derivation.
Its default Attic path pushes every named output and its runtime closure.
Attic's --stdin session starts upload work before EOF, so keeping one process
open allows partial progress with one client and one upload worker.
"""

import asyncio
import json
import os
import re
import signal
import sys
from asyncio.subprocess import Process
from contextlib import suppress

STORE_PATH = re.compile(r"/nix/store/[0-9abcdfghijklmnpqrsvwxyz]{32}-[^/\s]+\Z")
FLAKE = ".#atticPushTargets.x86_64-linux"


def completed_paths(line: bytes) -> list[str]:
    """Only successful BUILD records identify outputs safe to publish."""
    try:
        result = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid nix-fast-build JSON line") from exc
    if not isinstance(result, dict):
        raise ValueError("invalid nix-fast-build result")
    if result.get("type") != "BUILD" or result.get("success") is not True:
        return []
    outputs = result.get("outputs")
    if outputs is None or outputs == {}:
        return []  # nix-fast-build's Attic uploader also skips builds without outputs.
    if not isinstance(outputs, dict):
        raise ValueError("successful BUILD result has invalid outputs")
    paths = list(outputs.values())
    if any(not isinstance(path, str) or STORE_PATH.fullmatch(path) is None for path in paths):
        raise ValueError("successful BUILD result has an invalid store path")
    return paths


async def stop_process(process: Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except TimeoutError:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


async def build_and_push(cache: str) -> None:
    build_args = ["nix-fast-build", "--stream-json-lines", "--option", "pure-eval", "false"]
    if cache == "main":
        # public must not skip paths that are present only in main.
        build_args.append("--skip-cached")
    build_args.extend(["--flake", f"{FLAKE}.{cache}"])

    builder = await asyncio.create_subprocess_exec(*build_args, stdout=asyncio.subprocess.PIPE, start_new_session=True)
    uploader: Process | None = None
    upload_done: asyncio.Task[int] | None = None
    seen: set[str] = set()
    try:
        assert builder.stdout is not None
        while True:
            next_line = asyncio.create_task(builder.stdout.readline())
            try:
                done, _ = await asyncio.wait(
                    {next_line, upload_done} if upload_done is not None else {next_line},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if upload_done is not None and upload_done in done and next_line not in done:
                    raise RuntimeError(f"attic push exited early ({upload_done.result()})")
                line = next_line.result()
            finally:
                if not next_line.done():
                    next_line.cancel()
                    await asyncio.gather(next_line, return_exceptions=True)
            if not line:
                break
            for path in completed_paths(line):
                if path in seen:
                    continue
                if uploader is None:
                    uploader = await asyncio.create_subprocess_exec(
                        "attic",
                        "push",
                        "--stdin",
                        "--jobs",
                        "1",
                        f"ducktape:{cache}",
                        stdin=asyncio.subprocess.PIPE,
                        start_new_session=True,
                    )
                    upload_done = asyncio.create_task(uploader.wait())
                assert uploader.stdin is not None
                if upload_done is not None and upload_done.done():
                    raise RuntimeError(f"attic push exited early ({upload_done.result()})")
                uploader.stdin.write(f"{path}\n".encode())
                try:
                    await uploader.stdin.drain()
                except (BrokenPipeError, ConnectionResetError) as exc:
                    raise RuntimeError("attic push closed its input") from exc
                seen.add(path)

        build_status = await builder.wait()
        upload_status = 0
        if uploader is not None:
            assert uploader.stdin is not None
            assert upload_done is not None
            uploader.stdin.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await uploader.stdin.wait_closed()
            upload_status = await upload_done
        if build_status != 0 or upload_status != 0:
            raise RuntimeError(f"nix-fast-build exited {build_status}; attic push exited {upload_status}")
    except BaseException:
        await stop_process(builder)
        await stop_process(uploader)
        if upload_done is not None:
            await upload_done
        raise


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"main", "public"}:
        print(f"usage: {sys.argv[0]} {{main|public}}", file=sys.stderr)
        raise SystemExit(2)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task = loop.create_task(build_and_push(sys.argv[1]))
    received_signal: list[signal.Signals] = []

    def cancel(signum: signal.Signals) -> None:
        received_signal.append(signum)
        task.cancel()

    loop.add_signal_handler(signal.SIGTERM, cancel, signal.SIGTERM)
    loop.add_signal_handler(signal.SIGINT, cancel, signal.SIGINT)
    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        raise SystemExit(128 + received_signal[0] if received_signal else 1) from None
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"nix-attic-push: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        loop.close()


if __name__ == "__main__":
    main()
