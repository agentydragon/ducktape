"""Schema and runtime tests for the registered FastMCP exec tool."""

import pytest
import pytest_bazel
from fastmcp.client import Client

from mcp_infra.constants import WORKING_DIR
from mcp_infra.exec.docker.server import ContainerExecServer
from mcp_infra.exec.docker.types import AlwaysSetTo, ContainerExecServerConfig
from util.exec.models import MAX_EXEC_TIMEOUT_MS, BaseExecResult, Exited


@pytest.fixture
def restricted_exec_server(async_docker_client, debian_slim_image) -> ContainerExecServer:
    config = ContainerExecServerConfig(
        image=debian_slim_image,
        working_dir=WORKING_DIR,
        allow_user_field=False,
        allow_env_field=False,
        cwd_policy=AlwaysSetTo(value=WORKING_DIR),
    )
    return ContainerExecServer(async_docker_client, config)


async def test_registered_exec_tool_timeout_schema_and_runtime(docker_exec_server: ContainerExecServer) -> None:
    async with Client(docker_exec_server) as session:
        tools = await session.list_tools()
        exec_tool = next(tool for tool in tools if tool.name == ContainerExecServer.EXEC_TOOL_NAME)
        timeout_schema = exec_tool.input_schema["properties"]["timeout_ms"]

        assert timeout_schema["type"] == "integer"
        for forbidden in ("exclusiveMinimum", "exclusiveMaximum", "minimum", "maximum"):
            assert forbidden not in timeout_schema

        for timeout_ms in (0, MAX_EXEC_TIMEOUT_MS + 1):
            result = await session.call_tool(
                ContainerExecServer.EXEC_TOOL_NAME,
                {"cmd": ["true"], "timeout_ms": timeout_ms, "env": None, "user": None, "cwd": None},
                raise_on_error=False,
            )
            assert result.is_error

        result = await session.call_tool(
            ContainerExecServer.EXEC_TOOL_NAME,
            {"cmd": ["true"], "timeout_ms": MAX_EXEC_TIMEOUT_MS, "env": None, "user": None, "cwd": None},
        )
        assert not result.is_error
        execution = BaseExecResult.model_validate(result.structured_content)
        assert execution.exit == Exited(exit_code=0)


async def test_registered_exec_tool_applies_exposed_env_and_user(docker_exec_server: ContainerExecServer) -> None:
    async with Client(docker_exec_server) as session:
        tools = await session.list_tools()
        exec_tool = next(tool for tool in tools if tool.name == ContainerExecServer.EXEC_TOOL_NAME)
        properties = exec_tool.input_schema["properties"]
        assert {"env", "user"} <= properties.keys()

        env_result = await session.call_tool(
            ContainerExecServer.EXEC_TOOL_NAME,
            {
                "cmd": ["sh", "-c", "printf '%s' \"$EXEC_REAL_INPUT_TEST\""],
                "timeout_ms": 10000,
                "env": ["EXEC_REAL_INPUT_TEST=visible"],
                "user": None,
                "cwd": None,
            },
        )
        assert not env_result.is_error
        env_execution = BaseExecResult.model_validate(env_result.structured_content)
        assert env_execution.stdout == "visible"

        user_result = await session.call_tool(
            ContainerExecServer.EXEC_TOOL_NAME,
            {"cmd": ["id", "-u"], "timeout_ms": 10000, "env": None, "user": "65534", "cwd": None},
        )
        assert not user_result.is_error
        user_execution = BaseExecResult.model_validate(user_result.structured_content)
        assert user_execution.stdout == "65534\n"


async def test_registered_exec_tool_hides_unconfigured_fields_and_uses_fixed_cwd(
    restricted_exec_server: ContainerExecServer,
) -> None:
    async with Client(restricted_exec_server) as session:
        tools = await session.list_tools()
        exec_tool = next(tool for tool in tools if tool.name == ContainerExecServer.EXEC_TOOL_NAME)
        assert set(exec_tool.input_schema["properties"]) == {"cmd", "timeout_ms"}

        result = await session.call_tool(ContainerExecServer.EXEC_TOOL_NAME, {"cmd": ["pwd"], "timeout_ms": 10000})
        assert not result.is_error
        execution = BaseExecResult.model_validate(result.structured_content)
        assert execution.stdout == f"{WORKING_DIR}\n"

        for field_value in ({"env": ["EXEC_REAL_INPUT_TEST=hidden"]}, {"user": "65534"}):
            rejected = await session.call_tool(
                ContainerExecServer.EXEC_TOOL_NAME,
                {"cmd": ["true"], "timeout_ms": 10000, **field_value},
                raise_on_error=False,
            )
            assert rejected.is_error


if __name__ == "__main__":
    pytest_bazel.main()
