from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from mitmproxy.options import Options
from mitmproxy.tools.dump import DumpMaster
from mitmproxy.tools.main import mitmdump

from devinfra.js.debundle.live_proxy.addon import OPTIONS_ENV, DebundleLiveProxyAddon
from devinfra.js.debundle.live_proxy.core import (
    LiveProxyOptions,
    format_live_proxy_help,
    load_live_proxy_configuration,
    parse_live_proxy_args,
)

log = logging.getLogger(__name__)


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(datefmt="%Y-%m-%dT%H:%M:%S%z", format="%(asctime)s [%(levelname)s] %(message)s", level=level)


@dataclass(frozen=True)
class ProxyHandles:
    config: object


def main(argv: list[str] | None = None) -> None:
    configure_logging()
    options = parse_live_proxy_args(list(sys.argv[1:] if argv is None else argv))
    if options.help:
        print(format_live_proxy_help())
        return
    run_mitmdump(options)


def run_mitmdump(options: LiveProxyOptions) -> None:
    config = load_live_proxy_configuration(options)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.ca_dir.mkdir(parents=True, exist_ok=True)

    os.environ[OPTIONS_ENV] = json.dumps(options.to_json_dict())
    print_startup_summary(config)
    script_path = Path(__file__).with_name("mitmproxy_script.py")
    mitmdump_args = [
        "--listen-host",
        config.proxy_host,
        "--listen-port",
        str(config.proxy_port),
        "--set",
        f"confdir={config.ca_dir}",
        "-s",
        str(script_path),
    ]
    mitmdump(args=mitmdump_args)


@asynccontextmanager
async def start_proxy_in_process(options: LiveProxyOptions):
    config = load_live_proxy_configuration(options)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.ca_dir.mkdir(parents=True, exist_ok=True)

    mitm_options = Options(listen_host=config.proxy_host, listen_port=config.proxy_port, confdir=str(config.ca_dir))
    master = DumpMaster(mitm_options, with_termlog=False, with_dumper=False)
    master.addons.add(DebundleLiveProxyAddon(options))
    task = asyncio.create_task(master.run())
    try:
        await wait_until_listening(task, config.proxy_host, config.proxy_port)
        print_startup_summary(config)
        yield ProxyHandles(config=config)
    finally:
        master.shutdown()
        done, _pending = await asyncio.wait([task], timeout=5)
        if task in done:
            # Retrieve the result so a proxy failure isn't lost as a
            # "Task exception was never retrieved" warning. We're in teardown
            # (the body already ran), so log rather than mask any exception
            # already propagating out of the body.
            if (exc := task.exception()) is not None:
                log.warning("proxy task exited with error: %r", exc)
        else:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def wait_until_listening(task: asyncio.Task, host: str, port: int, timeout_s: float = 10.0) -> None:
    """Wait until the proxy accepts connections, or surface its startup failure.

    Races `wait_for_port` against the proxy `task` so that if `master.run()`
    fails during startup (e.g. the listen port can't be bound) the real error
    is re-raised promptly instead of timing out with a generic message while
    the task's exception is silently discarded.
    """
    port_ready = asyncio.ensure_future(wait_for_port(host, port, timeout_s))
    try:
        await asyncio.wait({task, port_ready}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            task.result()  # re-raises master.run()'s exception if it failed
            raise RuntimeError("proxy task exited before the listener was ready")
        await port_ready  # re-raise wait_for_port's error if it timed out
    finally:
        if not port_ready.done():
            port_ready.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await port_ready


async def wait_for_port(host: str, port: int, timeout_s: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    last_error: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            _reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError as error:
            last_error = error
            await asyncio.sleep(0.05)
    raise RuntimeError(f"proxy did not start listening on {host}:{port}") from last_error


def print_startup_summary(config) -> None:
    quick_start = shlex.join(
        [
            "chromium",
            f"--user-data-dir={config.profile_dir}",
            f"--proxy-server=http://{config.proxy_host}:{config.proxy_port}",
            "--ignore-certificate-errors",
            config.target_url,
        ]
    )
    log.info("proxy ready target=%s listen=http://%s:%s", config.target_origin, config.proxy_host, config.proxy_port)
    log.info("local assets prefix=%s", config.internal_prefix)
    log.info("bootstrap override=%s", config.bootstrap_url)
    log.info("mitm ca pem=%s", config.ca_dir / "mitmproxy-ca-cert.pem")
    log.info("browser profile dir=%s", config.profile_dir)
    log.info("quick-start browser command:")
    print(quick_start)
