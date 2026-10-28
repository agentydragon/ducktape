from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, overload

from pydantic import BaseModel, ValidationError

from x.wt.server.git_manager import GitManager
from x.wt.server.git_refs_watcher import GitRefsWatcher
from x.wt.server.github_watcher import GitHubWatcher
from x.wt.server.services import DiscoveryService, GitstatusdService, WorktreeCoordinator, WorktreeIndexService
from x.wt.server.worktree_service import WorktreeService
from x.wt.shared.configuration import Configuration
from x.wt.shared.protocol import ErrorCodes, ErrorResponse, Request, Response, create_error_response

logger = logging.getLogger(__name__)


class Stream:
    def __init__(self, writer: asyncio.StreamWriter | Any):
        self._writer = writer
        self._error_logged = False
        # TODO(mpokorny): consider async emit with backpressure for large/continuous streams

    def emit(self, event: BaseModel) -> None:
        if not self._writer:
            return
        self._writer.write((event.model_dump_json() + "\n").encode())


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any | None = None):
        super().__init__(message)
        self.code = code
        self.data = data


@dataclass(frozen=True)
class ServiceDependencies:
    config: Configuration
    git_manager: GitManager
    index: WorktreeIndexService
    gitstatusd: GitstatusdService
    github_watcher: GitHubWatcher | None
    git_refs_watcher: GitRefsWatcher
    discovery: DiscoveryService
    coordinator: WorktreeCoordinator
    worktree_service: WorktreeService


class RpcRegistry:
    def __init__(self) -> None:
        self._handlers: dict[
            str, Callable[[Request, ServiceDependencies, Any], Awaitable[Response | ErrorResponse]]
        ] = {}

    async def _invoke(
        self, req: Request, method: str, handler: Callable[[], Awaitable[Any]]
    ) -> Response | ErrorResponse:
        try:
            return Response(result=await handler(), id=req.id)
        except RpcError as e:
            return create_error_response(e.code, str(e), req.id, e.data)
        except Exception as e:
            logger.exception("Unhandled error in RPC method %s", method)
            return create_error_response(ErrorCodes.INTERNAL_ERROR, f"Internal error: {e}", req.id)

    @overload
    def method[ResultT](
        self, name: str
    ) -> Callable[
        [Callable[[ServiceDependencies], Awaitable[ResultT]]], Callable[[ServiceDependencies], Awaitable[ResultT]]
    ]: ...

    @overload
    def method[ParamsT: BaseModel, ResultT](
        self, name: str, *, params: type[ParamsT]
    ) -> Callable[
        [Callable[[ServiceDependencies, ParamsT], Awaitable[ResultT]]],
        Callable[[ServiceDependencies, ParamsT], Awaitable[ResultT]],
    ]: ...

    def method[ParamsT: BaseModel](self, name: str, *, params: type[ParamsT] | None = None) -> Any:
        if params is None:

            def decorate_no_params(handler: Callable[[ServiceDependencies], Awaitable[Any]]):
                async def wrapped(req: Request, deps: ServiceDependencies, writer: Any) -> Response | ErrorResponse:
                    return await self._invoke(req, name, lambda: handler(deps))

                self._handlers[name] = wrapped
                return handler

            return decorate_no_params

        def decorate_with_params(handler: Callable[[ServiceDependencies, ParamsT], Awaitable[Any]]):
            async def wrapped(req: Request, deps: ServiceDependencies, writer: Any) -> Response | ErrorResponse:
                try:
                    parsed = params.model_validate(req.params)
                except ValidationError as e:
                    return create_error_response(ErrorCodes.INVALID_PARAMS, str(e), req.id)
                return await self._invoke(req, name, lambda: handler(deps, parsed))

            self._handlers[name] = wrapped
            return handler

        return decorate_with_params

    def stream[ParamsT: BaseModel](
        self, name: str, *, params: type[ParamsT]
    ) -> Callable[
        [Callable[[ServiceDependencies, ParamsT, Stream], Awaitable[Any]]],
        Callable[[ServiceDependencies, ParamsT, Stream], Awaitable[Any]],
    ]:
        def decorate(handler: Callable[[ServiceDependencies, ParamsT, Stream], Awaitable[Any]]):
            async def wrapped(req: Request, deps: ServiceDependencies, writer: Any) -> Response | ErrorResponse:
                try:
                    parsed = params.model_validate(req.params)
                except ValidationError as e:
                    return create_error_response(ErrorCodes.INVALID_PARAMS, str(e), req.id)
                stream = Stream(writer)
                return await self._invoke(req, name, lambda: handler(deps, parsed, stream))

            self._handlers[name] = wrapped
            return handler

        return decorate

    def list_methods(self) -> list[str]:
        return list(self._handlers)

    async def dispatch(self, req: Request, deps: ServiceDependencies, writer: Any) -> Response | ErrorResponse:
        wrapped = self._handlers.get(req.method)
        if not wrapped:
            return create_error_response(ErrorCodes.METHOD_NOT_FOUND, f"Method '{req.method}' not found", req.id)
        return await wrapped(req, deps, writer)


rpc = RpcRegistry()
