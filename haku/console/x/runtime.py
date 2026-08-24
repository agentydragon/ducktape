"""Backend-neutral Console runtime catalog.

The sandbox and runner lifecycle is Haku infrastructure.  A runtime registration pairs that generic
infrastructure with the one harness adapter that knows how to launch and speak a provider's native
protocol.  The runner itself remains one Pydantic-envelope process bridge for every harness.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from haku.console.chat_models import RuntimeKind, TurnOutcome
from haku.console.x.conversation_events import ConversationEvent, Projection
from haku.console.x.sandbox_claims import SandboxClaims
from haku.console.x.system_prompt import SystemPromptTemplate
from haku.runtime.x.bridge.client import FrameSink, ReceivedFrame, SentPrompt
from haku.runtime.x.bridge.protocol import HarnessFrame, HarnessLaunch, TextWebSocket
from haku.runtime.x.bridge.transport import ProgressSink


class RuntimeClient(Protocol):
    """The provider-neutral part of a connected harness client used by the turn loop."""

    async def connect(self) -> Mapping[str, Any]: ...

    async def query(self, text: str) -> SentPrompt: ...

    async def interrupt(self) -> None: ...

    def frames(self) -> AsyncIterator[ReceivedFrame]: ...

    async def wait_closed(self) -> None: ...

    async def aclose(self) -> None: ...


RuntimeClientFactory = Callable[[TextWebSocket, HarnessLaunch, ProgressSink | None, FrameSink], RuntimeClient]


@dataclass(frozen=True, slots=True)
class RuntimeLaunch:
    """Generic facts a harness launch builder translates into its native argv/configuration."""

    cwd: str
    environment: Mapping[str, str]
    mcp_servers: Mapping[str, RuntimeMcpServer]
    appended_system_prompt: str | None
    resume_from: int | None


@dataclass(frozen=True, slots=True)
class RuntimeMcpServer:
    """One explicitly configured MCP capability available to a native harness."""

    url: str
    bearer_environment_variable: str


@dataclass(frozen=True, slots=True)
class TurnCompletion:
    """Provider-neutral interpretation of the native frame that ended a turn."""

    outcome: TurnOutcome
    final_text: str
    failure: str | None = None


@dataclass(frozen=True, slots=True)
class OpenItemSeed:
    """Durable part of an open prose item a replacement turn handler inherits."""

    text: str
    first_frame_seq: int
    last_frame_seq: int


@dataclass(frozen=True, slots=True)
class TurnProjectionSeed:
    """Provider-neutral durable facts from which one turn handler resumes."""

    open_message: OpenItemSeed | None = None
    open_reasoning: OpenItemSeed | None = None
    seen_call_ids: frozenset[str] = frozenset()
    completed_call_ids: frozenset[str] = frozenset()


EMPTY_TURN_PROJECTION_SEED = TurnProjectionSeed()


class Checkpoint(StrEnum):
    """Whether a frame's effects and projection cursor may commit yet."""

    ADVANCE = "advance"
    HOLD = "hold"


@dataclass(frozen=True, slots=True)
class FrameEffects:
    """One native frame's neutral effects, produced by its harness integration.

    ``events`` remain durable even when the same frame supplies ``completion``; the terminal write
    commits them atomically with the turn close. ``HOLD`` is for provider state that is not durably
    representable yet, such as partial JSON tool arguments. It is only valid with no emitted events
    or completion; replay then starts before the composition and the integration rebuilds its
    private state from the same raw frames.
    """

    events: tuple[ConversationEvent, ...] = ()
    completion: TurnCompletion | None = None
    checkpoint: Checkpoint = Checkpoint.ADVANCE

    def __post_init__(self) -> None:
        if self.checkpoint is Checkpoint.HOLD and self.events:
            raise ValueError("a held frame cannot emit durable conversation events")
        if self.checkpoint is Checkpoint.HOLD and self.completion is not None:
            raise ValueError("a terminal frame cannot hold the durable projection cursor")


class RuntimeTurnHandler(Protocol):
    """Stateful provider-owned interpretation of one turn's native frames."""

    def apply(self, *, frame_seq: int, frame: HarnessFrame) -> FrameEffects: ...


@dataclass(frozen=True, slots=True)
class WakeStart:
    """The harness began an exchange of its own while no turn was open.

    *description* is what woke it, in the harness's words where it gave any — a background task's
    completion notification, or the generic fallback where the wake carried no prose.
    """

    description: str


class RuntimeWakeWatcher(Protocol):
    """Provider-owned classification of frames arriving while no turn is open.

    Stateful across one idle span: the frames announcing a wake (a task notification, a command
    lifecycle marker) precede the frame that begins the exchange, so the watcher remembers what it
    has seen. `observe` returns a `WakeStart` for the frame that begins an exchange — that frame is
    then the opened turn's first — and None for every frame that is idle chatter.
    """

    def observe(self, frame: HarnessFrame) -> WakeStart | None: ...


class RuntimeAdapter(Protocol):
    """Provider-owned protocol behavior behind one immutable ``RuntimeKind``."""

    @property
    def kind(self) -> RuntimeKind: ...

    @property
    def display_name(self) -> str: ...

    def client(
        self, websocket: TextWebSocket, launch: RuntimeLaunch, progress: ProgressSink | None, frames_to: FrameSink
    ) -> RuntimeClient: ...

    def turn_handler(self, seed: TurnProjectionSeed = EMPTY_TURN_PROJECTION_SEED) -> RuntimeTurnHandler: ...

    def wake_watcher(self) -> RuntimeWakeWatcher | None:
        """A fresh watcher for one idle span, or None for a harness that never wakes itself.

        None is also the conservative answer: a provider whose idle-time frames are unclassified
        keeps the pre-wake behaviour, where the stream is only consumed inside a turn.
        """
        ...

    def project_log(self, frames: Iterable[tuple[int, HarnessFrame]]) -> Projection: ...

    def prompt_submitted(self, frames: Iterable[HarnessFrame]) -> bool:
        """Whether these outbound native frames include the turn's prompt submission."""
        ...


@dataclass(frozen=True, slots=True)
class RuntimeResources:
    """Generic runner/sandbox resources for one configured runtime implementation."""

    claims: SandboxClaims
    session_ttl_seconds: int
    cwd: str
    environment: Mapping[str, str]
    # Endpoints are runtime resources; authentication is minted per session and added only when
    # the authenticated runner connects. A configured runtime is therefore not bound to one Agent.
    mcp_server_urls: Mapping[str, str]
    system_prompt: SystemPromptTemplate


@dataclass(frozen=True, slots=True)
class ConfiguredRuntime:
    """One provider adapter paired with Haku-owned execution resources."""

    adapter: RuntimeAdapter
    resources: RuntimeResources


class UnsupportedRuntimeError(LookupError):
    """No adapter was registered for a conversation's immutable runtime kind."""


class RuntimeNotConfiguredError(RuntimeError):
    """A known runtime has no sandbox/credential configuration on this replica."""


class RuntimeRegistry:
    """Immutable runtime definitions plus the subset configured for execution.

    Read paths need only adapters.  A launch-capable Console registers resources separately for the
    same keys; absence is a registry fact rather than a half-initialized provider adapter.
    """

    def __init__(
        self,
        adapters: Mapping[RuntimeKind, RuntimeAdapter],
        resources: Mapping[RuntimeKind, RuntimeResources] | None = None,
    ):
        self._adapters = dict(adapters)
        self._resources = dict(resources or {})
        for kind, adapter in self._adapters.items():
            if adapter.kind is not kind:
                raise ValueError(f"runtime adapter key {kind!r} disagrees with adapter kind {adapter.kind!r}")
        unknown_resources = self._resources.keys() - self._adapters.keys()
        if unknown_resources:
            raise ValueError(f"runtime resources have no adapter: {sorted(kind.value for kind in unknown_resources)}")

    def adapter(self, kind: RuntimeKind) -> RuntimeAdapter:
        try:
            return self._adapters[kind]
        except KeyError as error:
            raise UnsupportedRuntimeError(f"runtime kind {kind!r} is not registered") from error

    def configured(self, kind: RuntimeKind) -> ConfiguredRuntime:
        adapter = self.adapter(kind)
        try:
            resources = self._resources[kind]
        except KeyError as error:
            raise RuntimeNotConfiguredError(f"runtime kind {kind!r} is not configured for execution") from error
        return ConfiguredRuntime(adapter=adapter, resources=resources)

    def __getitem__(self, kind: RuntimeKind) -> RuntimeAdapter:
        return self.adapter(kind)

    def __contains__(self, kind: RuntimeKind) -> bool:
        return kind in self._adapters

    @property
    def kinds(self) -> frozenset[RuntimeKind]:
        return frozenset(self._adapters)

    @property
    def configured_kinds(self) -> frozenset[RuntimeKind]:
        return frozenset(self._resources)

    async def aclose(self) -> None:
        """Close each configured claims client once, even if registrations share one."""
        closed: set[int] = set()
        for resources in self._resources.values():
            identity = id(resources.claims)
            if identity in closed:
                continue
            closed.add(identity)
            await resources.claims.aclose()
