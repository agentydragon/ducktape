"""Props-specific mock utilities."""

from collections.abc import Generator

from agent_core_testing.responses import DecoratorMock
from mcp_infra.exec.models import BaseExecResult
from openai_utils.model import FunctionCallItem, ResponsesRequest


class PropsMock(DecoratorMock):
    """Mock with props-specific helpers (psql, etc.)."""

    def psql_roundtrip(
        self, query: str, *, timeout_ms: int = 5000
    ) -> Generator[FunctionCallItem, ResponsesRequest, BaseExecResult]:
        """Execute psql query via docker exec and return result."""
        return self.docker_exec_roundtrip(["psql", "-c", query], timeout_ms=timeout_ms)
