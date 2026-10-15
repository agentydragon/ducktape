"""Python fixture setup for the Agentplane browser harness.

Tests compose setup (flags and scene builders on `window.agentplaneVisual`) and choose
the route before mounting React; subsequent interactions use ordinary Playwright locators.
"""

from enum import StrEnum
from typing import Literal

from playwright.async_api import Page, expect

IDLE_THREAD = "5f1c4a2e-0000-4000-8000-000000000001"
RUNNING_THREAD = "5f1c4a2e-0000-4000-8000-000000000002"
UNNAMED_THREAD = "5f1c4a2e-0000-4000-8000-000000000000"
SUSPENDED_THREAD = "5f1c4a2e-0000-4000-8000-000000000004"
DELETED_SANDBOX_THREAD = "5f1c4a2e-0000-4000-8000-000000000005"


class Flag(StrEnum):
    """A switch on the harness's mock services; the value is its name in harness.tsx's `Flag`."""

    CLAUDE_PAUSED = "claudePaused"
    WATCH_STALE = "watchStale"
    THREAD_DATABASE_DISCONNECTED = "threadDatabaseDisconnected"
    THREAD_STREAM_DROPPED = "threadStreamDropped"
    INVENTORY_STREAM_DROPPED = "inventoryStreamDropped"
    ENTITY_STREAM_DISCONNECTED = "entityStreamDisconnected"
    ENTITY_SEGMENTS_WITHHELD = "entitySegmentsWithheld"
    SYNC_SCOPE_UNAVAILABLE = "syncScopeUnavailable"
    COMMAND_ADMISSION_TIMED_OUT = "commandAdmissionTimedOut"
    PENDING_ACTIONS = "pendingActions"
    COMPACT_POD_ACTION = "compactPodAction"
    GMAIL_DRAFT_HISTORY = "gmailDraftHistory"
    PAGED_ACTION_HISTORY = "pagedActionHistory"
    ACTION_GROUPS_UNAVAILABLE = "actionGroupsUnavailable"
    NOTIFICATION_STATUS_UNAVAILABLE = "notificationStatusUnavailable"
    NOTIFICATION_EXTRA_ENTRY = "notificationExtraEntry"
    PUSH_BROWSER_ADDED = "pushBrowserAdded"
    CONNECTION_RENAMED = "connectionRenamed"
    MCP_LINKAGE_CHANGED = "mcpLinkageChanged"
    MCP_HEALTH_CHANGED = "mcpHealthChanged"


class Scene(StrEnum):
    """A fixture builder taking no arguments; the value is its name on `window.agentplaneVisual`."""

    ADD_PROVISIONING_SANDBOX = "addProvisioningSandbox"
    LONG_PENDING_ACTION = "longPendingAction"
    HIDDEN_CODEPOINTS = "hiddenCodepoints"
    ENDED_ATTACHMENT = "endedAttachment"
    INTERLEAVED_EVENTS = "interleavedEvents"
    LIFECYCLE_GROUP = "lifecycleGroup"
    THREAD_SETUP = "threadSetup"
    THREAD_SETUP_RUNNING = "threadSetupRunning"
    SHELL_CALLS = "shellCalls"
    MARKDOWN_CODE_FENCE = "markdownCodeFence"
    STREAMING_INTERLEAVED = "streamingInterleaved"
    UNFINISHED_REASONING = "unfinishedReasoning"
    COMMAND_PROGRESS = "commandProgress"
    PENDING_COMMANDS = "pendingCommands"
    PENDING_INPUT_ECHO = "pendingInputEcho"
    COMMAND_OUTCOMES = "commandOutcomes"
    REMEMBER_PENDING_INPUT = "rememberPendingInput"
    REMEMBER_INPUT_ECHO = "rememberInputEcho"
    REMEMBER_SETTLED_COMMANDS = "rememberSettledCommands"
    COMPLETED_ROLLOUT = "completedRollout"
    REPORTED_ROLLOUT = "reportedRollout"


class AgentplaneFixture:
    def __init__(self, page: Page) -> None:
        self.page = page

    async def mount_app(self, route: str) -> None:
        await self.page.evaluate("route => window.agentplaneVisual.mountApp(route)", route)
        await expect(self.page.locator("#app > *").first).to_be_attached()

    async def mount_thread(self, thread_id: str) -> None:
        await self.mount_app(f"/threads/{thread_id}")

    async def mount_disclosure(
        self,
        *,
        nested: bool = False,
        short: bool = False,
        open: bool = True,
        wrapped_headings: bool = False,
        tool_output: bool = False,
        output_open: bool = True,
        before_output: bool = True,
        after_output: bool = True,
        following_section: bool = False,
    ) -> None:
        await self.page.evaluate(
            "props => window.agentplaneVisual.mountDisclosure(props)",
            {
                "nested": nested,
                "short": short,
                "open": open,
                "wrappedHeadings": wrapped_headings,
                "toolOutput": tool_output,
                "outputOpen": output_open,
                "beforeOutput": before_output,
                "afterOutput": after_output,
                "followingSection": following_section,
            },
        )
        await expect(self.page.locator("#app > *").first).to_be_attached()

    async def age_outage(self, milliseconds: int) -> None:
        await self.page.evaluate("ms => window.agentplaneVisual.ageOutage(ms)", milliseconds)

    async def recovery(self, kind: Literal["messages", "tools", "quiet"]) -> None:
        await self.page.evaluate("kind => window.agentplaneVisual.recovery(kind)", kind)

    async def failed_turn(self, *, after_content: bool = False) -> None:
        await self.page.evaluate("after => window.agentplaneVisual.failedTurn(after)", after_content)

    async def standalone_reasoning(
        self, *, long_preview: bool = False, code_fence: bool = False, long_body: bool = False
    ) -> None:
        await self.page.evaluate(
            "([preview, code, body]) => window.agentplaneVisual.standaloneReasoning(preview, code, body)",
            [long_preview, code_fence, long_body],
        )

    async def standard_history(self, *, long_preview: bool = False, long_body: bool = False) -> None:
        await self.page.evaluate(
            "([preview, body]) => window.agentplaneVisual.standardHistory(preview, body)", [long_preview, long_body]
        )

    async def show_action_preview(self, group: str, name: str, arguments: dict[str, object]) -> None:
        await self.page.evaluate(
            "([action, args]) => window.agentplaneVisual.showActionPreview(action, args)",
            [{"group": group, "name": name}, arguments],
        )

    async def set_flag(self, flag: Flag, on: bool = True) -> None:
        await self.page.evaluate("([flag, on]) => window.agentplaneVisual.set(flag, on)", [flag, on])

    async def publish(self, flag: Flag, path_suffix: str) -> None:
        """Sets `flag` and serves the open streams whose path ends in `path_suffix` again."""
        await self.page.evaluate(
            "([flag, suffix]) => window.agentplaneVisual.publish(flag, suffix)", [flag, path_suffix]
        )

    async def queue_open_response(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.queueOpenResponse()")

    async def project_queued_session(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.projectQueuedSession()")

    async def build(self, scene: Scene) -> None:
        await self.page.evaluate("scene => window.agentplaneVisual[scene]()", scene)

    async def enable_suspended_sandbox_wake_on_message(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.enableSuspendedSandboxWakeOnMessage()")

    async def remember_dispatch(self, thread_id: str = RUNNING_THREAD) -> None:
        await self.page.evaluate("threadId => window.agentplaneVisual.rememberDispatch(threadId)", thread_id)

    async def remember_failed_dispatch(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.rememberFailedDispatch()")
