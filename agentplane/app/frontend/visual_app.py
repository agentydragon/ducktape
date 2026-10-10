"""Python fixture setup for the Agentplane browser harness.

These methods call concrete mock-data builders. There is no scene catalog, recipe
schema, or generic operation dispatcher. Tests compose setup and choose the route
before mounting React; subsequent interactions use ordinary Playwright locators.
"""

from typing import Literal

from playwright.async_api import Page, expect

IDLE_THREAD = "5f1c4a2e-0000-4000-8000-000000000001"
RUNNING_THREAD = "5f1c4a2e-0000-4000-8000-000000000002"
UNNAMED_THREAD = "5f1c4a2e-0000-4000-8000-000000000000"
SUSPENDED_THREAD = "5f1c4a2e-0000-4000-8000-000000000004"
DELETED_SANDBOX_THREAD = "5f1c4a2e-0000-4000-8000-000000000005"


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

    async def fail_grant_provisioning(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.failGrantProvisioning()")

    async def add_provisioning_sandbox(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.addProvisioningSandbox()")

    async def pause_claude(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.pauseClaude()")

    async def stale_watch(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.staleWatch()")

    async def disconnect_inventory_stream(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.disconnectInventoryStream()")

    async def show_pending_actions(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.showPendingActions()")

    async def show_compact_pod_action(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.showCompactPodAction()")

    async def show_action_preview(self, group: str, name: str, arguments: dict[str, object]) -> None:
        await self.page.evaluate(
            "([action, args]) => window.agentplaneVisual.showActionPreview(action, args)",
            [{"group": group, "name": name}, arguments],
        )

    async def paginate_action_history(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.paginateActionHistory()")

    async def publish_notification_change(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.publishNotificationChange()")

    async def publish_push_browser(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.publishPushBrowser()")

    async def publish_connection_rename(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.publishConnectionRename()")

    async def publish_mcp_linkage_change(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.publishMcpLinkageChange()")

    async def publish_mcp_health_change(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.publishMcpHealthChange()")

    async def set_notification_status_unavailable(self, unavailable: bool) -> None:
        await self.page.evaluate(
            "unavailable => window.agentplaneVisual.setNotificationStatusUnavailable(unavailable)", unavailable
        )

    async def fail_action_groups(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.failActionGroups()")

    async def time_out_command_admission(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.timeOutCommandAdmission()")

    async def long_pending_action(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.longPendingAction()")

    async def hidden_codepoints(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.hiddenCodepoints()")

    async def ended_attachment(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.endedAttachment()")

    async def interleaved_events(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.interleavedEvents()")

    async def lifecycle_group(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.lifecycleGroup()")

    async def thread_setup(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.threadSetup()")

    async def thread_setup_running(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.threadSetupRunning()")

    async def shell_calls(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.shellCalls()")

    async def markdown_code_fence(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.markdownCodeFence()")

    async def streaming_interleaved(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.streamingInterleaved()")

    async def unfinished_reasoning(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.unfinishedReasoning()")

    async def disconnect_thread_database(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.disconnectThreadDatabase()")

    async def disconnect_thread_stream(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.disconnectThreadStream()")

    async def fail_sync_scope(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.failSyncScope()")

    async def disconnect_entity_stream(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.disconnectEntityStream()")

    async def withhold_entity_segments(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.withholdEntitySegments()")

    async def command_progress(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.commandProgress()")

    async def pending_commands(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.pendingCommands()")

    async def pending_input_echo(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.pendingInputEcho()")

    async def command_outcomes(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.commandOutcomes()")

    async def remember_pending_input(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.rememberPendingInput()")

    async def remember_input_echo(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.rememberInputEcho()")

    async def remember_settled_commands(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.rememberSettledCommands()")

    async def completed_rollout(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.completedRollout()")

    async def reported_rollout(self) -> None:
        await self.page.evaluate("() => window.agentplaneVisual.reportedRollout()")
