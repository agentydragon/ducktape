from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from inspect import signature
from typing import Any, Protocol, get_origin, get_type_hints

from punq import Container
from pydantic import BaseModel, ValidationError

from wt.server.git_manager import GitManager
from wt.server.git_refs_watcher import GitRefsWatcher
from wt.server.github_watcher import GitHubWatcher
from wt.server.services import DiscoveryService, GitstatusdService, WorktreeCoordinator, WorktreeIndexService
from wt.server.worktree_service import WorktreeService
from wt.shared.configuration import Configuration
from wt.shared.protocol import ErrorCodes, ErrorResponse, Request, Response, create_error_response

logger = logging.getLogger(__name__)


class Emitter(Protocol):
    def emit(self, event: BaseModel) -> None: ...


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


Handler = Callable[..., Awaitable[Any]]


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


@dataclass(frozen=True)
class InvocationContext:
    deps: ServiceDependencies
    params_obj: BaseModel | None
    writer: Any
    start_time: datetime
    stream_obj: Stream | None = None


class RpcRegistry:
    def __init__(self) -> None:
        self._handlers: dict[
            str, Callable[[Request, ServiceDependencies, Any, datetime], Awaitable[Response | ErrorResponse]]
        ] = {}
        self._stream_methods: set[str] = set()

    def _build_args(self, fn, *, context: InvocationContext):
        deps = context.deps
        params_obj = context.params_obj
        start_time = context.start_time
        stream_obj = context.stream_obj
        sig = signature(fn)
        c = Container()

        # Core config and per-request context
        c.register(Configuration, instance=deps.config)
        c.register(datetime, instance=start_time)
        # Service singletons
        c.register(GitManager, instance=deps.git_manager)
        c.register(WorktreeIndexService, instance=deps.index)
        c.register(GitstatusdService, instance=deps.gitstatusd)
        if deps.github_watcher:
            c.register(GitHubWatcher, instance=deps.github_watcher)
        c.register(GitRefsWatcher, instance=deps.git_refs_watcher)
        c.register(DiscoveryService, instance=deps.discovery)
        c.register(WorktreeCoordinator, instance=deps.coordinator)
        c.register(WorktreeService, instance=deps.worktree_service)
        c.register(ServiceDependencies, instance=deps)
        args = []
        type_hints = get_type_hints(fn)
        for p in sig.parameters.values():
            anno = type_hints.get(p.name, p.annotation)
            if stream_obj is not None and (anno is Stream or get_origin(anno) is Stream):
                args.append(stream_obj)
            elif params_obj is not None and anno is type(params_obj):
                args.append(params_obj)

            else:
                args.append(c.resolve(anno))
        return args

    def _wrap_method[ParamsT: BaseModel](self, method: str, params_model: type[ParamsT] | None, handler) -> None:
        async def _wrapped(
            req: Request, deps: ServiceDependencies, writer, start_time: datetime
        ) -> Response | ErrorResponse:
            try:
                params = params_model.model_validate(req.params) if params_model is not None else None
            except ValidationError as e:
                return create_error_response(ErrorCodes.INVALID_PARAMS, str(e), req.id)

            try:
                args = self._build_args(
                    handler,
                    context=InvocationContext(deps=deps, params_obj=params, writer=writer, start_time=start_time),
                )
                result = await handler(*args)
                return Response(result=result, id=req.id)
            except RpcError as e:
                return create_error_response(e.code, str(e), req.id, e.data)
            except Exception as e:
                logger.exception("Unhandled error in method %s", method)
                return create_error_response(ErrorCodes.INTERNAL_ERROR, f"Internal error: {e}", req.id)

        self._handlers[method] = _wrapped

    def _wrap_stream[ParamsT: BaseModel](self, method: str, params_model: type[ParamsT], handler) -> None:
        async def _wrapped(
            req: Request, deps: ServiceDependencies, writer, start_time: datetime
        ) -> Response | ErrorResponse:
            try:
                params = params_model.model_validate(req.params)
            except ValidationError as e:
                return create_error_response(ErrorCodes.INVALID_PARAMS, str(e), req.id)
            try:
                stream = Stream(writer)
                args = self._build_args(
                    handler,
                    context=InvocationContext(
                        deps=deps, params_obj=params, writer=writer, start_time=start_time, stream_obj=stream
                    ),
                )
                result = await handler(*args)
                return Response(result=result, id=req.id)
            except RpcError as e:
                return create_error_response(e.code, str(e), req.id, e.data)
            except Exception as e:
                logger.exception("Unhandled error in stream method %s", method)
                return create_error_response(ErrorCodes.INTERNAL_ERROR, f"Internal error: {e}", req.id)

        self._handlers[method] = _wrapped
        self._stream_methods.add(method)

    def method[ParamsT: BaseModel](self, name: str, *, params: type[ParamsT] | None = None):
        def deco(fn: Callable[..., Awaitable[Any]]):
            # Allow DI-driven signatures; if params_model is provided,
            # function must accept a matching params type somewhere
            self._wrap_method(name, params, fn)
            return fn

        return deco

    def stream[ParamsT: BaseModel](self, name: str, *, params: type[ParamsT]):
        def deco(fn: Callable[..., Awaitable[Any]]):
            # Allow DI-driven signatures; DI resolver will inject api/config/params/stream
            self._wrap_stream(name, params, fn)
            return fn

        return deco

    def list_methods(self) -> list[str]:
        return list(self._handlers.keys())

    async def dispatch(
        self, req: Request, deps: ServiceDependencies, writer, start_time: datetime
    ) -> Response | ErrorResponse:
        wrapped = self._handlers.get(req.method)
        if not wrapped:
            return create_error_response(ErrorCodes.METHOD_NOT_FOUND, f"Method '{req.method}' not found", req.id)
        return await wrapped(req, deps, writer, start_time)


rpc = RpcRegistry()
