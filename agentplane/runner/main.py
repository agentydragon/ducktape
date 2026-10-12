"""The runner process: the sessions in a state directory, driven over the control stream it dials to
the Runner Gateway, their Events pushed to the ingress it dials.

Credentials come from the runner's own environment, never from flags or the protocol:
ANTHROPIC_AUTH_TOKEN for Claude sessions, OPENAI_API_KEY for Codex sessions. A harness child
inherits nothing implicitly: its environment is what --harness-env declares, so a variable the
runner holds reaches the child only when the deployment names it -- those two keys above all stay
with the runner. --harness-inherit-env names one of those held variables without setting a value
for it, for the case where the image and not the deployment owns the value.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated

import typer

from agentplane.runner.config import ClaudeLaunch, CodexLaunch, DebugCheckpoint, RunnerConfig, ServiceEndpoint
from agentplane.runner.control import ControlTiming, RunnerControlClient
from agentplane.runner.ingress import IngressPusher
from agentplane.runner.service import Runner

logger = logging.getLogger(__name__)
app = typer.Typer(add_completion=False)


def harness_environment(
    environ: Mapping[str, str], *, declared: Sequence[str], inherited: Sequence[str] = ()
) -> dict[str, str]:
    """The environment every harness child starts from, as --harness-env and --harness-inherit-env
    gave it: `NAME=value` sets the variable, a bare `NAME` takes the runner's own value and is
    absent when the runner has none (`docker run -e` and Bazel's --action_env read the two forms the
    same way). Entries apply in order, so a later one wins.

    `inherited` is the bare form spelled on its own flag, for names whose value the *image* owns
    (TZDIR, say) and the deployment merely forwards. It applies first, so a deployment that also
    sets one explicitly still wins. A name the runner was started without is reported rather than
    silently dropped: a value that goes missing from the image is otherwise indistinguishable from
    a correctly absent variable, and the child just behaves as if the zone were unrecognised.
    """
    for name in inherited:
        if "=" in name:
            raise ValueError(f"--harness-inherit-env takes a bare NAME, got {name!r}")
    child: dict[str, str] = {}
    for entry in [*inherited, *declared]:
        name, separator, value = entry.partition("=")
        if not name:
            raise ValueError(f"--harness-env expects NAME or NAME=value, got {entry!r}")
        if separator:
            child[name] = value
        elif name in environ:
            child[name] = environ[name]
    for name in inherited:
        if name not in child:
            logger.warning("inherited %s is absent from the runner environment: harnesses will not see it", name)
    return child


@app.command()
def main(
    state_dir: Annotated[Path, typer.Option(help="Session logs and harness persistence live here.")],
    control_target: Annotated[str, typer.Option(help="Runner Gateway runner-control host:port to dial.")],
    control_bearer: Annotated[
        str, typer.Option(help="Bearer placeholder the egress proxy substitutes on the runner control stream.")
    ],
    ingress_target: Annotated[str, typer.Option(help="Runner ingress host:port to push every Session's Events to.")],
    ingress_bearer: Annotated[
        str, typer.Option(help="Bearer placeholder the egress proxy substitutes on the runner ingress.")
    ],
    claude_binary: Annotated[Path | None, typer.Option(help="Claude Code CLI; omit to refuse Claude sessions.")] = None,
    anthropic_base_url: Annotated[str | None, typer.Option(help="Anthropic Messages endpoint for Claude.")] = None,
    codex_binary: Annotated[Path | None, typer.Option(help="Codex CLI; omit to refuse Codex sessions.")] = None,
    openai_base_url: Annotated[
        str | None, typer.Option(help="OpenAI Responses base URL, including /v1, for Codex.")
    ] = None,
    harness_env: Annotated[
        list[str] | None,
        typer.Option(
            "--harness-env",
            help="NAME=value a harness child starts with, or a bare NAME to take the runner's own "
            "value; repeat per variable.",
        ),
    ] = None,
    harness_inherit_env: Annotated[
        list[str] | None,
        typer.Option(
            "--harness-inherit-env",
            help="Bare NAME of a variable this process holds that every harness child should start "
            "with, its value owned by the image rather than by this invocation; repeat per name.",
        ),
    ] = None,
    test_debug_checkpoint_name: Annotated[
        str | None, typer.Option(hidden=True, help="Test-only crash-test checkpoint name.")
    ] = None,
    test_debug_checkpoint_command_id: Annotated[
        str | None, typer.Option(hidden=True, help="Test-only crash-test checkpoint command id.")
    ] = None,
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    claude = None
    if claude_binary is not None:
        if anthropic_base_url is None:
            raise typer.BadParameter("--anthropic-base-url is required with --claude-binary")
        claude = ClaudeLaunch(
            binary=claude_binary, base_url=anthropic_base_url, auth_token=os.environ["ANTHROPIC_AUTH_TOKEN"]
        )
    codex = None
    if codex_binary is not None:
        if openai_base_url is None:
            raise typer.BadParameter("--openai-base-url is required with --codex-binary")
        codex = CodexLaunch(binary=codex_binary, base_url=openai_base_url, api_key=os.environ["OPENAI_API_KEY"])
    if (test_debug_checkpoint_name is None) != (test_debug_checkpoint_command_id is None):
        raise typer.BadParameter("both test debug checkpoint options must be set together")
    config = RunnerConfig(
        state_dir=state_dir,
        harness_environment=harness_environment(
            os.environ, declared=harness_env or [], inherited=harness_inherit_env or []
        ),
        claude=claude,
        codex=codex,
        test_debug_checkpoint=(
            DebugCheckpoint(test_debug_checkpoint_name, test_debug_checkpoint_command_id)
            if test_debug_checkpoint_name is not None and test_debug_checkpoint_command_id is not None
            else None
        ),
    )
    asyncio.run(
        async_main(
            config,
            control=ServiceEndpoint(target=control_target, bearer=control_bearer),
            ingress=ServiceEndpoint(target=ingress_target, bearer=ingress_bearer),
        )
    )


async def async_main(config: RunnerConfig, *, control: ServiceEndpoint, ingress: ServiceEndpoint) -> None:
    async with Runner(config) as runner:
        await runner.startup()
        stopping = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stopping.set)
        # A broken control stream or ingress push must not take the harnesses down with it, nor each other.
        dialing = [
            asyncio.create_task(
                RunnerControlClient(runner, target=control.target, bearer=control.bearer, timing=ControlTiming()).run(),
                name="runner control stream",
            ),
            asyncio.create_task(
                IngressPusher(runner, target=ingress.target, bearer=ingress.bearer, timing=ControlTiming()).run(),
                name="ingress push",
            ),
        ]
        for task in dialing:
            task.add_done_callback(_report_dialing_end)
        await stopping.wait()
        for task in dialing:
            task.remove_done_callback(_report_dialing_end)
            task.cancel()
        await asyncio.gather(*dialing, return_exceptions=True)
        logger.info("stopping: %d session(s)", len(runner.sessions))


def _report_dialing_end(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and (error := task.exception()) is not None:
        logger.error("%s stopped", task.get_name(), exc_info=error)


if __name__ == "__main__":
    app()
