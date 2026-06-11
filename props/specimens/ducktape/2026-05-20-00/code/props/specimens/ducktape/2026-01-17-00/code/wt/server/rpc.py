from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from inspect import signature
from typing import TYPE_CHECKING, Any, Protocol, get_origin, get_type_hints

from punq import Container
from pydantic import BaseModel, ValidationError

from ..shared.configuration import Configuration
from ..shared.protocol import ErrorCodes, ErrorResponse, Request, Response, create_error_response
from .git_manager import GitManager
from .git_refs_watcher import GitRefsWatcher
from .github_watcher import GitHubWatcher
from .services import DiscoveryService, GitstatusdService, WorktreeCoordinator, WorktreeIndexService
from .worktree_service import WorktreeService

if TYPE_CHECKING:
    from .wt_server import WtDaemon

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


@dataclass(frozen=True)
class InvocationContext:
    daemon: WtDaemon
    params_obj: BaseModel | None
    writer: Any
    start_time: datetime
    stream_obj: Stream | None = None


class RpcRegistry:
    def __init__(self) -> None:
        self._handlers: dict[
            str, Callable[[Request, WtDaemon, Any, datetime], Awaitable[Response | ErrorResponse]]
        ] = {}
        self._stream_methods: set[str] = set()

    def _build_args(self, fn, *, context: InvocationContext):
        daemon = context.daemon
        params_obj = context.params_obj
        start_time = context.start_time
        stream_obj = context.stream_obj
        sig = signature(fn)
        c = Container()

        # Core config and per-request context
        c.register(Configuration, instance=daemon.config)
        c.register(datetime, instance=start_time)
        # Service singletons wired from daemon
        c.register(GitManager, instance=daemon.git_manager)
        c.register(WorktreeIndexService, instance=daemon.index_service)
        c.register(GitstatusdService, instance=daemon.gitstatusd_service)
        if daemon.github_watcher:
            c.register(GitHubWatcher, instance=daemon.github_watcher)
        c.register(GitRefsWatcher, instance=daemon.git_refs_watcher)
        c.register(DiscoveryService, instance=daemon.discovery_service)
        c.register(WorktreeCoordinator, instance=daemon)  # WtDaemon implements WorktreeCoordinator
        # Also expose WorktreeService for orchestration flows (imported at module level)
        c.register(WorktreeService, instance=daemon.worktree_service)
        c.register(
            ServiceDependencies,
            instance=ServiceDependencies(
                config=daemon.config,
                git_manager=daemon.git_manager,
                index=daemon.index_service,
                gitstatusd=daemon.gitstatusd_service,
                github_watcher=daemon.github_watcher,
                git_refs_watcher=daemon.git_refs_watcher,
                discovery=daemon.discovery_service,
                coordinator=daemon,
            ),
        )
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
        async def _wrapped(req: Request, daemon: WtDaemon, writer, start_time: datetime) -> Response | ErrorResponse:
            try:
                params = params_model.model_validate(req.params) if params_model is not None else None
            except ValidationError as e:
                return create_error_response(ErrorCodes.INVALID_PARAMS, str(e), req.id)

            try:
                args = self._build_args(
                    handler,
                    context=InvocationContext(daemon=daemon, params_obj=params, writer=writer, start_time=start_time),
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
        async def _wrapped(req: Request, daemon: WtDaemon, writer, start_time: datetime) -> Response | ErrorResponse:
            try:
                params = params_model.model_validate(req.params)
            except ValidationError as e:
                return create_error_response(ErrorCodes.INVALID_PARAMS, str(e), req.id)
            try:
                stream = Stream(writer)
                args = self._build_args(
                    handler,
                    context=InvocationContext(
                        daemon=daemon, params_obj=params, writer=writer, start_time=start_time, stream_obj=stream
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

    async def dispatch(self, req: Request, daemon: WtDaemon, writer, start_time: datetime) -> Response | ErrorResponse:
        wrapped = self._handlers.get(req.method)
        if not wrapped:
            return create_error_response(ErrorCodes.METHOD_NOT_FOUND, f"Method '{req.method}' not found", req.id)
        return await wrapped(req, daemon, writer, start_time)


rpc = RpcRegistry()
