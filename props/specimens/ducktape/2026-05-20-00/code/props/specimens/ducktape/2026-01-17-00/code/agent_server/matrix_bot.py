from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlencode

import aiodocker
import typer
from fastmcp.client import Client
from pydantic import TypeAdapter

from agent_core.agent import Agent
from agent_core.loop_control import RequireAnyTool
from agent_server.logging_config import configure_logging_info
from agent_server.server.bus import ServerBus, UiEndTurn
from agent_server.server.mode_handler import ServerModeHandler
from mcp_infra.compositor.notifications_buffer import NotificationsBuffer
from mcp_infra.compositor.server import Compositor
from mcp_infra.config_loader import build_mcp_config
from mcp_infra.display.event_renderer import DisplayEventsHandler
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.exec.container_session import ContainerOptions
from mcp_infra.exec.docker.server import ContainerExecServer
from mcp_infra.exec.models import BaseExecResult, make_exec_input
from mcp_infra.mcp_types import NetworkMode
from mcp_infra.mounted import Mounted
from mcp_infra.prefix import MCPMountPrefix
from openai_utils.client_factory import build_client
from openai_utils.model import SystemMessage, UserMessage


class MatrixBotCompositor(Compositor):
    """Compositor with runtime and matrix_control servers pre-mounted."""

    runtime: Mounted[ContainerExecServer]
    matrix_control: Mounted[EnhancedFastMCP]

    def __init__(
        self,
        docker_client: aiodocker.Docker,
        docker_image: str,
        network_mode: NetworkMode,
        environment: dict[str, str],
        bus: ServerBus,
    ):
        super().__init__()
        self._docker_client = docker_client
        self._docker_image = docker_image
        self._network_mode = network_mode
        self._environment = environment
        self._bus = bus

    async def __aenter__(self):
        await super().__aenter__()

        # Mount runtime server (docker exec)
        self.runtime = await self.mount_inproc(
            MCPMountPrefix("runtime"),
            ContainerExecServer(
                self._docker_client,
                ContainerOptions(
                    image=self._docker_image,
                    network_mode=self._network_mode,
                    environment=self._environment,
                    labels={"adgn.project": "matrix-bot", "adgn.role": "runtime"},
                ),
            ),
            pinned=True,
        )

        # Mount matrix control server (inlined from make_matrix_control_server)
        matrix_control = EnhancedFastMCP(
            "Matrix Control Server", instructions="Matrix control: yield-only control to signal end of turn."
        )

        @matrix_control.flat_model()
        def do_yield() -> UiEndTurn:
            """End the current turn. The runner will wake you on new DMs."""
            self._bus.push_end_turn()
            return UiEndTurn()

        self.matrix_control = await self.mount_inproc(MCPMountPrefix("matrix_control"), matrix_control, pinned=True)

        return self


app = typer.Typer(help="Matrix-driven Agent entrypoint (docker + yield-only control)", no_args_is_help=True)


@app.command()
def run(
    model: str = typer.Option(os.getenv("OPENAI_MODEL", "gpt-5.1-codex-mini"), "--model"),
    mcp_configs: list[Path] = typer.Option(  # noqa: B008
        [], "--mcp-config", exists=True, file_okay=True, dir_okay=False, readable=True, resolve_path=True
    ),
    homeserver: str = typer.Option(..., "--homeserver", help="Matrix homeserver base URL"),
    user_id: str = typer.Option(..., "--user-id", help="Matrix user id (e.g. @bot:example.com)"),
    access_token: str = typer.Option(..., "--access-token", help="Matrix access token"),
    room: str = typer.Option(..., "--room", help="Room id or alias to watch (#alias:server or !id:server)"),
    docker_image: str = typer.Option(os.getenv("MATRIX_DOCKER_IMAGE", "curlimages/curl:8.8.0"), "--docker-image"),
    network_mode: str = typer.Option(os.getenv("MATRIX_DOCKER_NETWORK", "bridge"), "--network"),
    system: str | None = typer.Option(None, "--system", help="Override default system instructions"),
    initial_since: str | None = typer.Option(os.getenv("MATRIX_SINCE"), "--since"),
) -> None:
    """Run Agent in headless Matrix mode using docker_exec + yield-only control."""

    async def _run() -> None:
        configure_logging_info(set_stream_handler_level=False)

        _ = build_mcp_config(mcp_configs)
        ui_bus = ServerBus()

        nm = NetworkMode(network_mode) if network_mode in ("none", "bridge", "host") else NetworkMode.BRIDGE
        env = {
            "MATRIX_BASE_URL": homeserver,
            "MATRIX_ACCESS_TOKEN": access_token,
            "MATRIX_ROOM_ID": room,
            "MATRIX_USER_ID": user_id,
        }

        client = build_client(model)

        # Build MatrixBotCompositor with runtime + matrix control servers
        docker_client = aiodocker.Docker()
        try:
            async with MatrixBotCompositor(
                docker_client=docker_client, docker_image=docker_image, network_mode=nm, environment=env, bus=ui_bus
            ) as comp:
                # Build tool names from mounted servers (after __aenter__)
                docker_exec_tool = comp.runtime.tool_name(comp.runtime.server.exec_tool)
                matrix_yield_tool = comp.matrix_control.tool_name(comp.matrix_control.server.do_yield)

                effective_system = (system or "").strip() or (
                    "You are a Matrix-driven assistant. Do not emit plain text.\n"
                    "I/O contract:\n"
                    f"- Use {docker_exec_tool} to call Matrix HTTP APIs (curl) or your CLI from inside the container.\n"
                    f"- Read new DMs, send replies, and when finished call {matrix_yield_tool}().\n"
                    "- Do not emit plain text; only use tools.\n"
                )

                # Client with notifications buffer so UI can reflect MCP updates
                notif_buffer = NotificationsBuffer(compositor=comp)
                async with Client(comp, message_handler=notif_buffer.handler) as mcp_client:
                    agent = await Agent.create(
                        mcp_client=mcp_client,
                        client=client,
                        handlers=[
                            ServerModeHandler(bus=ui_bus, poll_notifications=notif_buffer.poll),
                            DisplayEventsHandler(),
                        ],
                        tool_policy=RequireAnyTool(),
                    )
                    agent.process_message(SystemMessage.text(effective_system))

                    async def _sync_once(since: str | None) -> tuple[str, bool]:
                        """Poll Matrix sync API and return (next_since, has_new_events)."""
                        qs = {"timeout": "30000"}
                        if since:
                            qs["since"] = since
                        url = f"$MATRIX_BASE_URL/_matrix/client/v3/sync?{urlencode(qs)}"
                        curl_cmd = (
                            f'curl -sS -H "Authorization: Bearer $MATRIX_ACCESS_TOKEN" --fail --max-time 35 "{url}"'
                        )

                        # Call docker exec using helper with defaults
                        exec_input = make_exec_input(["sh", "-lc", curl_cmd], timeout_ms=40_000)
                        res = await mcp_client.session.call_tool(
                            name=docker_exec_tool, arguments=exec_input.model_dump()
                        )
                        exec_result = TypeAdapter(BaseExecResult).validate_python(res.structured_content or {})

                        stdout = exec_result.stdout or ""
                        assert isinstance(stdout, str), "Matrix API response should not be truncated"

                        data = json.loads(stdout)
                        next_since = data.get("next_batch") or (since or "")
                        rooms = (data.get("rooms") or {}).get("join") or {}
                        events = (rooms.get(room) or {}).get("timeline", {}).get("events", [])
                        return next_since, bool(events)

                    since_token = initial_since or None
                    while True:
                        next_since, has_new = await _sync_once(since_token)
                        since_token = next_since
                        if not has_new:
                            continue
                        agent.process_message(UserMessage.text("process matrix inbox"))
                        await agent.run()
        finally:
            await docker_client.close()

    asyncio.run(_run())


def main() -> None:
    app()
