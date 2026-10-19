// @vitest-environment happy-dom
import { describe, expect, it } from "vitest";

import { CallToolResultView, type CallToolResult, parseCallToolResult } from "../call_tool_result";
import { mount, SSH_EXEC_ARGUMENTS, sshExec } from "../testing";
import {
  actionOffersCardDecisionButtons,
  renderActionCall,
  renderActionLabel,
  renderActionOutcome,
  renderDetailsArguments,
  renderDetailsResult,
  renderNativeDetailsResult,
  renderPaneCollapsed,
  renderPaneOpened,
  shouldRenderPaneRequestTitle,
} from "./index";

const SSH_EXEC = { group: "ssh", name: "exec" };

async function drawDetailsResult(action: { group: string; name: string }, result: CallToolResult) {
  const custom = renderDetailsResult(action, result);
  return mount(custom === undefined ? <CallToolResultView result={result} /> : custom);
}

/** The CallToolResult stored for an ssh `exec` call whose `ExecResult` has `returned`'s fields. */
function stored(returned: Record<string, unknown> = {}): CallToolResult {
  const result = parseCallToolResult(sshExec("succeeded", returned).execution?.result);
  if (result === null) throw new Error("the fixture is not a CallToolResult");
  return result;
}

describe("Action presentation slots", () => {
  it("uses a distinct collapsed-pane view from the opened-pane and details views", async () => {
    const args = {
      namespace: "prod",
      fieldSelector: "status.phase=Running",
      labelSelector: "app=web",
    };
    const collapsed = await mount(
      renderPaneCollapsed({ group: "kubernetes_admin", name: "pods_list_in_namespace" }, args)
    );
    const opened = await mount(renderPaneOpened({ group: "kubernetes_admin", name: "pods_list_in_namespace" }, args));
    const details = await mount(
      renderDetailsArguments({ group: "kubernetes_admin", name: "pods_list_in_namespace" }, args)
    );

    expect(collapsed.textContent).toContain("Matching pods where");
    expect(collapsed.textContent).not.toContain("namespace prod");
    expect(opened.textContent).toContain("field selector status.phase=Running");
    expect(opened.textContent).toContain("label selector app=web");
    expect(opened.textContent).not.toContain("List pods in namespace");
    expect(opened.textContent).not.toContain("namespace prod");
    expect(details.textContent).toContain("field selector status.phase=Running");
    expect(details.textContent).not.toContain("List pods in namespace");
    expect(details.textContent).not.toContain("namespace prod");
  });

  it("uses a human-facing Action label in place of the technical identity", async () => {
    const action = { group: "kubernetes_admin", name: "pods_list_in_namespace" };
    const args = { namespace: "tofu-controller" };
    const label = await mount(renderActionLabel(action, args));

    expect(label.textContent).toContain("List pods in namespace tofu-controller");
    expect(label.textContent).not.toContain("kubernetes_admin");
    expect(label.textContent).not.toContain("pods_list_in_namespace");
    const fallback = await mount(renderActionLabel(action, { namespace: "tofu-controller", hidden: true }));
    expect(fallback.textContent).toContain("List pods in namespace");
    expect(shouldRenderPaneRequestTitle(action, args, "List pods in namespace tofu-controller")).toBe(false);
    expect(shouldRenderPaneRequestTitle(action, args, "Inspect the running demo pods")).toBe(true);
  });

  it.each([
    ["resources_get", { apiVersion: "v1", kind: "Pod", name: "web-0", namespace: "apps" }, "Get Pod apps/web-0"],
    ["resources_delete", { apiVersion: "v1", kind: "Pod", name: "web-0", namespace: "apps" }, "Delete Pod apps/web-0"],
    ["pods_delete", { name: "web-0", namespace: "apps" }, "Delete Pod apps/web-0"],
    ["pods_exec", { name: "web-0", namespace: "apps", command: ["true"] }, "Run command in Pod apps/web-0"],
    ["pods_get", { name: "web-0", namespace: "apps" }, "Get Pod apps/web-0"],
    ["pods_log", { name: "web-0", namespace: "apps" }, "View logs for Pod apps/web-0"],
  ] as const)("uses the concise Kubernetes identity for %s", async (name, args, expected) => {
    const label = await mount(renderActionLabel({ group: "kubernetes_admin", name }, args));
    expect(label.textContent).toContain(expected);
    expect(label.textContent).not.toContain("in namespace apps");
  });

  it("keeps a Kubernetes target in its heading and shows only additional details below it", async () => {
    const action = { group: "kubernetes_admin", name: "resources_get" };
    const args = { apiVersion: "apps/v1", kind: "Deployment", name: "web", namespace: "apps" };
    const label = await mount(renderActionLabel(action, args));
    const opened = await mount(renderPaneOpened(action, args));
    const details = await mount(renderDetailsArguments(action, args));

    expect(label.textContent).toContain("Get Deployment apps/web");
    expect(opened.textContent).toContain("Read with API version apps/v1");
    expect(details.textContent).toContain("Read with API version apps/v1");
    expect(details.textContent).not.toContain("Get Deployment");
    expect(details.textContent).not.toContain("apps/web");
  });

  it("keeps the target in the label without adding namespace boilerplate", async () => {
    const action = { group: "kubernetes_admin", name: "resources_get" };
    const args = { apiVersion: "v1", kind: "Pod", name: "web-0" };
    const opened = await mount(renderPaneOpened(action, args));
    const details = await mount(renderDetailsArguments(action, args));
    const label = await mount(renderActionLabel(action, { ...args, namespace: "apps" }));
    const suppliedNamespace = await mount(renderPaneOpened(action, { ...args, namespace: "apps" }));

    expect(opened.textContent).toContain("Read with API version v1");
    expect(details.textContent).toContain("Read with API version v1");
    expect(opened.textContent).not.toContain("Namespace");
    expect(details.textContent).not.toContain("Namespace");
    expect(suppliedNamespace.textContent).not.toContain("Namespace");
    expect(label.textContent).toContain("apps/web-0");
    expect(actionOffersCardDecisionButtons(action, { ...args, namespace: "" })).toBe(false);
  });

  it("puts list scope in Kubernetes action labels and leaves only filters in the pane", async () => {
    const listArgs = { apiVersion: "v1", kind: "Pod", namespace: "apps", labelSelector: "app=web" };
    const listAction = { group: "kubernetes_admin", name: "resources_list" };
    const listLabel = await mount(renderActionLabel(listAction, listArgs));
    const listCollapsed = await mount(renderPaneCollapsed(listAction, listArgs));
    const listPane = await mount(renderPaneOpened(listAction, listArgs));
    expect(listLabel.textContent).toContain("List Pod resources in namespace apps");
    expect(listCollapsed.textContent).toContain("label selector app=web");
    expect(listCollapsed.textContent).not.toContain("API version");
    expect(listCollapsed.textContent).not.toContain("apps");
    expect(listPane.textContent).toContain("label selector app=web");
    expect(listPane.textContent).toContain("Using API version v1");
    expect(listPane.textContent).not.toContain("apps");

    const eventsArgs = { namespace: "apps", fieldSelector: "type=Warning" };
    const eventsAction = { group: "kubernetes_admin", name: "events_list" };
    const eventsLabel = await mount(renderActionLabel(eventsAction, eventsArgs));
    const eventsCollapsed = await mount(renderPaneCollapsed(eventsAction, eventsArgs));
    const eventsPane = await mount(renderPaneOpened(eventsAction, eventsArgs));
    expect(eventsLabel.textContent).toContain("List events in namespace apps");
    expect(eventsCollapsed.textContent).toContain("Show events matching");
    expect(eventsCollapsed.textContent).toContain("type=Warning");
    expect(eventsCollapsed.textContent).not.toContain("apps");
    expect(eventsPane.textContent).toContain("Show events matching");
    expect(eventsPane.textContent).toContain("type=Warning");
    expect(eventsPane.textContent).not.toContain("apps");
  });

  it("shows calendar context and explicit delete grace in the collapsed pane", async () => {
    const calendar = await mount(
      renderPaneCollapsed(
        { group: "tana", name: "get_or_create_calendar_node" },
        { workspaceId: "workspace-123", granularity: "week", date: "2026-10-12" }
      )
    );
    expect(calendar.textContent).toContain("week calendar node");
    expect(calendar.textContent).toContain("2026-10-12");
    expect(calendar.textContent).toContain("workspace-123");

    const deletion = await mount(
      renderPaneCollapsed(
        { group: "kubernetes_admin", name: "resources_delete" },
        { apiVersion: "v1", kind: "Pod", name: "web-0", namespace: "apps", gracePeriodSeconds: 0 }
      )
    );
    expect(deletion.textContent).toContain("Request a 0s grace period");
    expect(deletion.textContent).not.toContain("Use API version v1");
    expect(deletion.textContent).not.toContain("apps/web-0");
  });

  it("uses SSH's custom pane and full-details argument renderers", async () => {
    const collapsed = await mount(renderPaneCollapsed(SSH_EXEC, SSH_EXEC_ARGUMENTS));
    const opened = await mount(renderPaneOpened(SSH_EXEC, SSH_EXEC_ARGUMENTS));
    const details = await mount(renderDetailsArguments(SSH_EXEC, SSH_EXEC_ARGUMENTS));

    expect(collapsed.textContent).toContain("$ echo test-output");
    expect(opened.textContent).toContain("test-user@test-host.example");
    expect(details.textContent).toContain("test-user@test-host.example");
  });

  it("renders Gmail drafts from submitted data without repeating their subject", async () => {
    const action = { group: "gmail", name: "drafts_create" };
    const args = {
      to: ["reader@example.com"],
      cc: ["copy@example.com"],
      subject: "Release notes",
      body: "The release is ready.\nPlease review it.",
      thread_id: "thread-123",
    };
    const label = await mount(renderActionLabel(action, args));
    const collapsed = await mount(renderPaneCollapsed(action, args));
    const opened = await mount(renderPaneOpened(action, args));
    const details = await mount(renderDetailsArguments(action, args));

    expect(label.textContent).toContain("Gmail: Draft email");
    expect(collapsed.textContent).toContain("reader@example.com");
    expect(collapsed.textContent).toContain("Release notes");
    expect(collapsed.textContent).toContain("The release is ready.");
    expect(opened.textContent).toContain("copy@example.com");
    expect(opened.textContent).toContain("The release is ready.");
    expect(details.textContent).toContain("Please review it.");
    expect(details.textContent).toContain("Release notes");

    const result = parseCallToolResult({ content: [], structuredContent: { id: "draft-123" }, isError: false });
    if (result === null) throw new Error("the fixture is not a CallToolResult");
    const renderedResult = await mount(renderDetailsResult(action, result));
    expect(renderedResult.textContent).toContain("Draft created");
    expect(
      renderedResult.querySelector('a[href="https://mail.google.com/mail/u/0/#drafts?compose=draft-123"]')
    ).not.toBeNull();
  });

  it("keeps a Gmail draft's request and created draft in one call view", async () => {
    const action = { group: "gmail", name: "drafts_create" };
    const args = {
      to: ["reader@example.com"],
      cc: ["copy@example.com"],
      bcc: ["blind-copy@example.com"],
      subject: "Release notes",
      body: "The release is ready.\nPlease review it.",
      thread_id: "thread-123",
    };
    const pending = renderActionCall(action, args);
    expect(pending?.includesResult).toBe(false);
    const pendingView = await mount(pending?.content ?? null);
    expect(pendingView.textContent).toContain("Release notes");
    expect(pendingView.textContent).toContain("reader@example.com");
    expect(pendingView.textContent).toContain("blind-copy@example.com");
    expect(pendingView.textContent).toContain("Please review it.");
    expect(pendingView.querySelector('a[href="https://mail.google.com/mail/u/0/#all/thread-123"]')).not.toBeNull();

    const result = parseCallToolResult({
      content: [],
      structuredContent: {
        id: "draft-123",
        message: { payload: { headers: [{ name: "Subject", value: "Release notes saved" }] } },
      },
      isError: false,
    });
    if (result === null) throw new Error("the fixture is not a CallToolResult");
    const completed = renderActionCall(action, args, result);
    expect(completed?.includesResult).toBe(true);
    const completedView = await mount(completed?.content ?? null);
    expect(completedView.textContent).toContain("Release notes saved");
    expect(completedView.textContent).toContain("reader@example.com");
    expect(completedView.textContent).toContain("The release is ready.");
    expect(completedView.textContent).toContain("draft draft-123");
    expect(
      completedView.querySelector('a[href="https://mail.google.com/mail/u/0/#drafts?compose=draft-123"]')
    ).not.toBeNull();
    expect(renderActionCall(action, { ...args, unexpected: true })).toBeUndefined();

    const unexpectedResult = parseCallToolResult({
      content: [],
      structuredContent: { id: 123 },
      isError: false,
    });
    if (unexpectedResult === null) throw new Error("the fixture is not a CallToolResult");
    expect(renderActionCall(action, args, unexpectedResult)).toBeUndefined();
  });

  it("renders Gmail search arguments and results without fetching subjects", async () => {
    const action = { group: "gmail", name: "threads_list" };
    const args = { q: "from:alerts@example.com", maxResults: 5, includeSpamTrash: false };
    const label = await mount(renderActionLabel(action, args));
    const collapsed = await mount(renderPaneCollapsed(action, args));
    const opened = await mount(renderPaneOpened(action, args));

    expect(label.textContent).toContain("Gmail: Search threads");
    expect(collapsed.textContent).toContain("from:alerts@example.com");
    expect(collapsed.textContent).not.toContain("maxResults");
    expect(collapsed.textContent).not.toContain("Including spam and trash");
    expect(opened.textContent).toContain('"maxResults": 5');
    expect(opened.textContent).toContain('"includeSpamTrash": false');

    const includingSpamAndTrash = await mount(
      renderPaneCollapsed(action, { q: "from:alerts@example.com", includeSpamTrash: true })
    );
    expect(includingSpamAndTrash.textContent).toContain("Including spam and trash");
    expect(includingSpamAndTrash.textContent).toContain("Search for");
    const continuationPage = await mount(
      renderPaneCollapsed(action, { q: "from:alerts@example.com", pageToken: "secret-token" })
    );
    expect(continuationPage.textContent).toContain("continuation page");
    expect(continuationPage.textContent).not.toContain("secret-token");

    const result = parseCallToolResult({
      content: [],
      structuredContent: {
        threads: [{ id: "thread-1", snippet: "Your build completed." }],
        nextPageToken: "next",
      },
      isError: false,
    });
    if (result === null) throw new Error("the fixture is not a CallToolResult");
    const renderedResult = await mount(renderDetailsResult(action, result));
    expect(renderedResult.textContent).toContain("Your build completed.");
    expect(renderedResult.textContent).toContain("More threads available");
    expect(renderedResult.querySelector('a[href="https://mail.google.com/mail/u/0/#all/thread-1"]')).not.toBeNull();
  });

  it("renders Grocy list options and results without resolving IDs", async () => {
    for (const [name, args, expected] of [
      ["products_list", { detail: "full" }, "Full Product records"],
      ["quantity_units_list", {}, "Quantity unit names"],
    ] as const) {
      const action = { group: "grocy_sf", name };
      const opened = await mount(renderPaneOpened(action, args));
      const details = await mount(renderDetailsArguments(action, args));
      expect(opened.textContent).toContain(expected);
      expect(details.textContent).toContain(expected);
    }

    const productsLabel = await mount(
      renderActionLabel({ group: "grocy_sf", name: "products_list" }, { detail: "full" })
    );
    const unitsLabel = await mount(renderActionLabel({ group: "grocy_sf", name: "quantity_units_list" }, {}));
    const systemInfoLabel = await mount(renderActionLabel({ group: "grocy_sf", name: "get_system_info" }, {}));
    expect(productsLabel.textContent).toContain("List Grocy products");
    expect(unitsLabel.textContent).toContain("List quantity units");
    expect(systemInfoLabel.textContent).toContain("Show Grocy system information");
  });

  it("renders Tana calendar arguments from submitted data without resolving the workspace", async () => {
    const action = { group: "tana", name: "get_or_create_calendar_node" };
    const args = { workspaceId: "workspace-123", granularity: "week", date: "2026-10-12" };
    const opened = await mount(renderPaneOpened(action, args));
    const details = await mount(renderDetailsArguments(action, args));
    const label = await mount(renderActionLabel(action, args));

    expect(label.textContent).toContain("Get or create Tana calendar node");

    for (const rendered of [opened, details]) {
      expect(rendered.textContent).toContain("week");
      expect(rendered.textContent).toContain("2026-10-12");
      expect(rendered.textContent).toContain("workspace-123");
    }
  });

  it("does not register any action whose Haku renderer depended on lookups", async () => {
    const excluded = [
      ["gmail", "threads_modify_labels"],
      ["gmail", "threads_get"],
      ["gmail", "messages_get"],
      ["google_calendar", "create_event"],
      ["google_calendar", "update_event"],
      ["google_calendar", "get_event"],
      ["google_calendar", "list_events"],
      ["google_calendar", "list_event_instances"],
      ["google_calendar", "delete_event"],
      ["grocy_sf", "stock_add"],
      ["grocy_sf", "stock_consume"],
      ["grocy_sf", "stock_entry_edit"],
      ["grocy_sf", "stock_get"],
      ["grocy_sf", "products_create"],
      ["grocy_sf", "products_edit"],
      ["grocy_sf", "shopping_list_get"],
      ["grocy_sf", "shopping_list_items_add"],
      ["grocy_sf", "shopping_list_items_remove"],
      ["grocy_sf", "shopping_list_item_edit"],
      ["tana", "import_tana_paste"],
      ["tana", "trash_node"],
      ["tana", "edit_node"],
      ["tana", "move_node"],
      ["tana", "set_field_option"],
    ] as const;
    const result = parseCallToolResult({
      content: [],
      structuredContent: { lookup_dependent_value: "preserve raw response" },
      isError: false,
    });
    if (result === null) throw new Error("the fixture is not a CallToolResult");

    for (const [group, name] of excluded) {
      const action = { group, name };
      expect(renderActionLabel(action, { id: "opaque-id" })).toBeUndefined();
      expect(renderPaneCollapsed(action, { id: "opaque-id" })).toBeUndefined();
      expect(renderPaneOpened(action, { id: "opaque-id" })).toBeUndefined();
      expect(renderDetailsArguments(action, { id: "opaque-id" })).toBeUndefined();

      const raw = await drawDetailsResult(action, result);
      expect(raw.textContent).toContain("Structured content");
      expect(raw.textContent).toContain('"lookup_dependent_value": "preserve raw response"');
    }
  });

  it("summarizes a Gmail draft with its recipient, subject, and body preview", async () => {
    const collapsed = await mount(
      renderPaneCollapsed(
        { group: "gmail", name: "drafts_create" },
        { to: ["user@example.com"], subject: "Deploy", body: "Ready" }
      )
    );
    expect(collapsed.textContent).toContain("To user@example.com");
    expect(collapsed.textContent).toContain("Deploy");
    expect(collapsed.textContent).toContain("Ready");
  });

  it("leaves unregistered slots to the host and preserves arguments rejected by a strict schema", async () => {
    expect(renderPaneCollapsed(SSH_EXEC, { ...SSH_EXEC_ARGUMENTS, test_extra: true })).toBeUndefined();
    expect(renderPaneOpened({ group: "test_group", name: "exec" }, SSH_EXEC_ARGUMENTS)).toBeUndefined();
    const recorded = await mount(renderDetailsArguments(SSH_EXEC, { ...SSH_EXEC_ARGUMENTS, test_extra: true }));
    expect(recorded.textContent).toContain("Details shown as recorded data");
    expect(renderDetailsArguments({ group: "__proto__", name: "constructor" }, {})).toBeUndefined();
  });

  it("shows recorded Kubernetes arguments when the strict schema does not accept them", async () => {
    const action = { group: "kubernetes_admin", name: "resources_get" };
    const args = { apiVersion: "v1", kind: "Pod", name: "web-0", namespace: "apps", futureField: true };
    const label = await mount(renderActionLabel(action, args));
    const details = await mount(renderDetailsArguments(action, args));

    expect(label.textContent).toContain("Get Kubernetes resource");
    expect(details.textContent).toContain("Details shown as recorded data");
    expect(details.textContent).toContain('"futureField": true');
    expect(actionOffersCardDecisionButtons(action, args)).toBe(false);
  });

  it("keeps pod-delete arguments schema-bound while omitting the empty pretty view", async () => {
    const action = { group: "kubernetes_admin", name: "pods_delete" };
    expect(renderDetailsArguments(action, { name: "web-0", namespace: "apps" })).toBeNull();

    const recorded = await mount(
      renderDetailsArguments(action, { name: "web-0", namespace: "apps", futureField: true })
    );
    expect(recorded.textContent).toContain("Details shown as recorded data");
    expect(recorded.textContent).toContain('"futureField": true');
  });

  it.each([
    ["resources_get", { apiVersion: "apps/v1", kind: "Deployment", name: "api" }, ["Read with API version apps/v1"]],
    ["pods_log", { name: "api-0", tail: -1 }, ['"tail": -1']],
  ] as const)(
    "uses the opened-pane argument widget for %s when optional fields are omitted",
    async (name, args, visible) => {
      const container = await mount(renderPaneOpened({ group: "kubernetes_admin", name }, args));
      for (const value of visible) expect(container.textContent).toContain(value);
    }
  );
});

describe("details result rendering", () => {
  it("draws the tool's value with the Action's widget", async () => {
    const container = await mount(renderDetailsResult(SSH_EXEC, stored()));
    expect(container.textContent).toContain("Exit 0");
    expect(container.textContent).not.toContain("Structured content");
  });

  it("draws the result as the tool answered when the widget does not take its value", async () => {
    const container = await drawDetailsResult(SSH_EXEC, stored({ exit_code: "test-not-a-number" }));
    expect(container.textContent).toContain("Structured content");
    expect(container.textContent).toContain('"exit_code": "test-not-a-number"');
    expect(container.textContent).not.toContain("Exit");
  });

  it("draws an error result as the tool answered", async () => {
    const failure = { kind: "target_not_configured", message: "SSH host/user target is not configured" };
    const result = parseCallToolResult({ content: [{ type: "text", text: JSON.stringify(failure) }], isError: true });
    if (result === null) throw new Error("the fixture is not a CallToolResult");
    const container = await drawDetailsResult(SSH_EXEC, result);
    expect(container.textContent).toContain("Tool error");
    expect(container.textContent).toContain('"kind": "target_not_configured"');
  });

  it("draws an unregistered Action's result as the tool answered", async () => {
    const container = await drawDetailsResult({ group: "test_group", name: "exec" }, stored());
    expect(container.textContent).toContain("Structured content");
    expect(container.textContent).not.toContain("Exit 0");
  });

  it("formats Kubernetes YAML result text with its structured value only once", async () => {
    const action = { group: "kubernetes_admin", name: "resources_get" };
    const resource = {
      apiVersion: "v1",
      kind: "ConfigMap",
      metadata: { name: "settings", namespace: "apps" },
      data: { mode: "safe" },
    };
    const result = parseCallToolResult({
      content: [
        {
          type: "text",
          text: "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: settings\n  namespace: apps\ndata:\n  mode: safe",
        },
      ],
      structuredContent: resource,
      isError: false,
    });
    if (result === null) throw new Error("the fixture is not a CallToolResult");

    const container = await mount(renderDetailsResult(action, result));
    expect(container.textContent).toContain("Resource · ConfigMap apps/settings");
    expect(container.textContent?.match(/kind: ConfigMap/g)).toHaveLength(1);
    expect(container.textContent).not.toContain("Structured content");
    expect(container.querySelector(".cm-editor")).not.toBeNull();
  });

  it("formats a single-line YAML apply argument and shows malformed input verbatim", async () => {
    const action = { group: "kubernetes_admin", name: "resources_create_or_update" };
    const manifest =
      "{apiVersion: v1, kind: ConfigMap, metadata: {name: settings, namespace: apps}, data: {mode: safe}}";
    const formatted = await mount(renderDetailsArguments(action, { resource: manifest }));
    expect(formatted.textContent).toContain("Manifest · ConfigMap apps/settings");
    expect(formatted.textContent).toContain("apiVersion: v1");
    expect(formatted.querySelector(".cm-editor")).not.toBeNull();

    const malformed = await mount(renderDetailsArguments(action, { resource: "kind: [broken" }));
    expect(malformed.textContent).toContain("Manifest · showing submitted text");
    expect(malformed.textContent).toContain("kind: [broken");
  });

  it("summarizes applied resources by their supplied identities", async () => {
    const action = { group: "kubernetes_admin", name: "resources_create_or_update" };
    const single = await mount(
      renderPaneCollapsed(action, {
        resource: "{apiVersion: v1, kind: ConfigMap, metadata: {name: settings, namespace: apps}}",
      })
    );
    expect(single.textContent).toContain("Apply ConfigMap apps/settings");

    const multiple = await mount(
      renderPaneCollapsed(action, {
        resource:
          "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: settings\n  namespace: apps\n---\napiVersion: v1\nkind: Secret\nmetadata:\n  name: token\n  namespace: apps\n",
      })
    );
    expect(multiple.textContent).toContain("Apply 2 Kubernetes resources: ConfigMap apps/settings, Secret apps/token");

    const unnamed = await mount(renderPaneCollapsed(action, { resource: "apiVersion: v1\nkind: Service\n" }));
    expect(unnamed.textContent).toContain("Apply Service");

    const malformed = await mount(renderPaneCollapsed(action, { resource: "kind: [broken" }));
    expect(malformed.textContent).toContain("Kubernetes manifest");
  });

  it("renders a returned YAML sequence as separate Kubernetes resources", async () => {
    const action = { group: "kubernetes_admin", name: "resources_create_or_update" };
    const result = parseCallToolResult({
      content: [
        {
          type: "text",
          text: [
            "- apiVersion: v1",
            "  kind: ConfigMap",
            "  metadata:",
            "    name: settings",
            "    namespace: apps",
            "- apiVersion: v1",
            "  kind: Secret",
            "  metadata:",
            "    name: token",
            "    namespace: apps",
          ].join("\n"),
        },
      ],
      isError: false,
    });
    if (result === null) throw new Error("the fixture is not a CallToolResult");

    const container = await mount(renderDetailsResult(action, result));
    expect(container.textContent).toContain("Resource 1 · ConfigMap apps/settings");
    expect(container.textContent).toContain("Resource 2 · Secret apps/token");
    expect(container.querySelectorAll(".cm-editor")).toHaveLength(2);
  });

  it("shows an exact delete success as an outcome with no fabricated result body", async () => {
    const action = { group: "kubernetes_admin", name: "resources_delete" };
    const args = {
      apiVersion: "v1",
      kind: "Pod",
      name: "web-0",
      namespace: "apps",
      gracePeriodSeconds: 0,
    };
    const success = parseCallToolResult({
      content: [{ type: "text", text: "Resource deleted successfully" }],
      isError: false,
    });
    if (success === null) throw new Error("the fixture is not a CallToolResult");

    expect(renderDetailsResult(action, success)).toBeNull();
    const call = renderActionCall(action, args, success);
    expect(call?.includesResult).toBe(true);
    expect(renderActionOutcome(action, success)?.label).toBe("Deletion accepted");
    expect((await mount(call?.content)).textContent).toContain("request a 0s grace period");
    const argumentsView = await mount(renderDetailsArguments(action, args));
    expect(argumentsView.textContent).toContain("Use API version v1");
    expect(argumentsView.textContent).toContain("request a 0s grace period");

    const warning = parseCallToolResult({
      content: [{ type: "text", text: "Resource deleted successfully, but finalizers are still running" }],
      isError: false,
    });
    if (warning === null) throw new Error("the fixture is not a CallToolResult");
    expect(renderDetailsResult(action, warning)).toBeUndefined();
    expect(renderActionOutcome(action, warning)).toBeUndefined();
    const warningView = await drawDetailsResult(action, warning);
    expect(warningView.textContent).toContain("finalizers are still running");
    expect(renderActionCall(action, args, warning)).toBeUndefined();
  });

  it("shows an exact pod-delete success as accepted and preserves non-success envelopes", async () => {
    const action = { group: "kubernetes_admin", name: "pods_delete" };
    const success = parseCallToolResult({
      content: [{ type: "text", text: "Pod deleted successfully" }],
      isError: false,
    });
    if (success === null) throw new Error("the fixture is not a CallToolResult");

    expect(renderDetailsResult(action, success)).toBeNull();
    expect(renderActionOutcome(action, success)).toEqual({ label: "Deletion accepted", failed: false });

    const warning = parseCallToolResult({
      content: [
        { type: "text", text: "Pod deleted successfully" },
        { type: "text", text: "Warning: cleanup is still running" },
      ],
      isError: false,
    });
    if (warning === null) throw new Error("the fixture is not a CallToolResult");
    expect(renderDetailsResult(action, warning)).toBeUndefined();
    expect(renderActionOutcome(action, warning)).toBeUndefined();
    const warningView = await drawDetailsResult(action, warning);
    expect(warningView.textContent).toContain("Pod deleted successfully");
    expect(warningView.textContent).toContain("Warning: cleanup is still running");

    const additionalContent = parseCallToolResult({
      content: [
        { type: "text", text: "Pod deleted successfully" },
        { type: "future", payload: "keep this content" },
      ],
      structuredContent: "Pod deleted successfully",
      isError: false,
    });
    if (additionalContent === null) throw new Error("the fixture is not a CallToolResult");
    expect(renderDetailsResult(action, additionalContent)).toBeUndefined();
    expect(renderActionOutcome(action, additionalContent)).toEqual({ label: "Deletion accepted", failed: false });
    const additionalContentView = await drawDetailsResult(action, additionalContent);
    expect(additionalContentView.textContent).toContain("Pod deleted successfully");
    expect(additionalContentView.textContent).toContain("keep this content");

    const mismatch = parseCallToolResult({
      content: [{ type: "text", text: "Pod not found" }],
      isError: false,
    });
    if (mismatch === null) throw new Error("the fixture is not a CallToolResult");
    expect(renderDetailsResult(action, mismatch)).toBeUndefined();
    expect(renderActionOutcome(action, mismatch)).toBeUndefined();
    expect((await drawDetailsResult(action, mismatch)).textContent).toContain("Pod not found");

    const error = parseCallToolResult({
      content: [{ type: "text", text: "Pod deleted successfully" }],
      isError: true,
    });
    if (error === null) throw new Error("the fixture is not a CallToolResult");
    expect(renderDetailsResult(action, error)).toBeUndefined();
    expect(renderActionOutcome(action, error)).toBeUndefined();
    const errorView = await drawDetailsResult(action, error);
    expect(errorView.textContent).toContain("Tool error");
    expect(errorView.textContent).toContain("Pod deleted successfully");
  });
});

const cardDecisionButtonCases: Array<{
  group: string;
  name: string;
  args: Record<string, unknown>;
  visible: string[];
}> = [
  {
    group: "kubernetes_admin",
    name: "resources_get",
    args: { apiVersion: "apps/v1", kind: "Deployment", name: "api" },
    visible: ["Read with API version apps/v1"],
  },
  {
    group: "kubernetes_admin",
    name: "pods_list_in_namespace",
    args: { namespace: "prod", fieldSelector: "status.phase=Running", labelSelector: "app=web" },
    visible: ["Matching pods where", "status.phase=Running", "app=web"],
  },
  {
    group: "kubernetes_admin",
    name: "resources_list",
    args: { apiVersion: "v1", kind: "Pod", fieldSelector: "status.phase=Running", labelSelector: "app=web" },
    visible: ["Using API version v1", "status.phase=Running", "app=web"],
  },
  {
    group: "kubernetes_admin",
    name: "pods_log",
    args: { name: "api-0", container: "sidecar", previous: true, tail: -1 },
    visible: ['"container": "sidecar"', '"previous": true', '"tail": -1'],
  },
  {
    group: "kubernetes_admin",
    name: "resources_delete",
    args: { apiVersion: "v1", kind: "Pod", name: "api-0", namespace: "prod", gracePeriodSeconds: 0 },
    visible: ["request a 0s grace period"],
  },
  {
    group: "kubernetes_admin",
    name: "events_list",
    args: { namespace: "prod", fieldSelector: "type=Warning" },
    visible: ["Show events matching", "type=Warning"],
  },
  { group: "grocy_sf", name: "products_list", args: { detail: "full" }, visible: ["Full Product records"] },
  { group: "grocy_sf", name: "quantity_units_list", args: {}, visible: ["Quantity unit names"] },
  {
    group: "gmail",
    name: "threads_list",
    args: { q: "from:deployments@example.com", maxResults: 5 },
    visible: ["from:deployments@example.com", '"maxResults": 5'],
  },
  {
    group: "github",
    name: "create_pull_request",
    args: {
      owner: "example",
      repo: "repo",
      title: "Update docs",
      head: "feature",
      base: "devel",
      body: "",
      draft: true,
      maintainer_can_modify: false,
      reviewers: ["reviewer1"],
    },
    visible: [
      "example/repo",
      "Update docs",
      "from feature",
      "into devel",
      "Create as a draft.",
      "Do not allow maintainers to edit the branch.",
      "Request review from reviewer1.",
    ],
  },
];

describe("opened-pane renderers", () => {
  it.each(cardDecisionButtonCases.filter(({ name }) => name !== "pods_list_in_namespace"))(
    "draws all displayed request fields for $group/$name",
    async ({ group, name, args, visible }) => {
      const container = await mount(renderPaneOpened({ group, name }, args));
      for (const value of visible) expect(container.textContent).toContain(value);
    }
  );

  it("shows an explicitly supplied maintainer edit permission in compact and opened PR summaries", async () => {
    const action = { group: "github", name: "create_pull_request" };
    const args = {
      owner: "example",
      repo: "repo",
      title: "Update docs",
      head: "feature",
      base: "devel",
      body: "",
      maintainer_can_modify: true,
    };
    const collapsed = await mount(renderPaneCollapsed(action, args));
    const opened = await mount(renderPaneOpened(action, args));

    expect(collapsed.textContent).toContain("Allow maintainers to edit the branch.");
    expect(opened.textContent).toContain("Allow maintainers to edit the branch.");
  });
});

describe("card decision buttons capability", () => {
  it.each(cardDecisionButtonCases.filter(({ name }) => name !== "resources_delete"))(
    "validates the whole $group/$name request independently",
    ({ group, name, args }) => {
      expect(actionOffersCardDecisionButtons({ group, name }, args)).toBe(true);
      expect(actionOffersCardDecisionButtons({ group, name }, { ...args, invisible: "must review" })).toBe(false);
    }
  );

  it("withholds card decision buttons from a Gmail search the card cannot fully show", () => {
    const search = { group: "gmail", name: "threads_list" };
    expect(actionOffersCardDecisionButtons(search, { q: "label:inbox" })).toBe(true);
    expect(actionOffersCardDecisionButtons(search, { q: "label:inbox", pageToken: "next" })).toBe(false);
    expect(actionOffersCardDecisionButtons(search, { q: "label:inbox", includeSpamTrash: true })).toBe(false);
  });

  it("never offers card decision buttons for a delete", () => {
    expect(
      actionOffersCardDecisionButtons(
        { group: "kubernetes_admin", name: "resources_delete" },
        { apiVersion: "v1", kind: "Pod", name: "api-0", namespace: "prod" }
      )
    ).toBe(false);
  });

  it("requires full review for PR descriptions, hidden defaults, and unknown actions", () => {
    const pr = cardDecisionButtonCases.find((candidate) => candidate.group === "github");
    if (pr === undefined) throw new Error("missing pull request fixture");
    expect(
      actionOffersCardDecisionButtons({ group: pr.group, name: pr.name }, { ...pr.args, body: "important text" })
    ).toBe(false);
    expect(
      actionOffersCardDecisionButtons({ group: pr.group, name: pr.name }, { ...pr.args, title: "x".repeat(121) })
    ).toBe(false);
    expect(
      actionOffersCardDecisionButtons(
        { group: pr.group, name: pr.name },
        { ...pr.args, reviewers: Array(5).fill("reviewer") }
      )
    ).toBe(false);
    expect(actionOffersCardDecisionButtons(SSH_EXEC, SSH_EXEC_ARGUMENTS)).toBe(false);
    expect(actionOffersCardDecisionButtons({ group: "__proto__", name: "constructor" }, {})).toBe(false);
  });

  it("still renders a long PR description for review while withholding the card decision buttons", async () => {
    const pr = cardDecisionButtonCases.find((candidate) => candidate.group === "github");
    if (pr === undefined) throw new Error("missing pull request fixture");
    const args = { ...pr.args, body: "important text" };

    expect(actionOffersCardDecisionButtons({ group: pr.group, name: pr.name }, args)).toBe(false);
    const container = await mount(renderPaneOpened({ group: pr.group, name: pr.name }, args));
    expect(container.textContent).toContain("important text");
  });
});

describe("sandbox rendering", () => {
  const action = (name: string) => ({ group: "sandbox", name });
  const info = {
    name: "waiting-sandbox",
    template: "python",
    phase: "Pending",
    conditions: [{ type: "Ready", status: "False", reason: "ContainersNotReady", message: "Image pulling" }],
  };
  const result = (value: unknown): unknown => value;

  it.each([
    ["create", { name: "new-sandbox", template: "python" }, "Create sandbox", "Template: python"],
    ["get", { name: "waiting-sandbox" }, "Inspect sandbox", "No additional arguments."],
    ["dispose", { name: "disposed-sandbox" }, "Dispose sandbox", "No additional arguments."],
    ["list", {}, "List sandboxes", "No arguments."],
    [
      "exec",
      { name: "script-sandbox", script: 'echo "$HOME"', timeout_seconds: 60, max_output_bytes: 0, cwd: "/workspace" },
      "Run script in sandbox",
      "Output limit 0 bytes per stream",
    ],
  ])("renders %s without authorizing compact decisions", async (name, args, label, body) => {
    expect((await mount(renderActionLabel(action(name), args))).textContent).toContain(label);
    expect((await mount(renderPaneOpened(action(name), args))).textContent).toContain(body);
    expect(actionOffersCardDecisionButtons(action(name), args)).toBe(false);
    const recorded = await mount(renderDetailsArguments(action(name), { ...args, hidden: true }));
    expect(recorded.textContent).toContain("Details shown as recorded data");
  });

  it("shows readiness conditions and empty lists", async () => {
    const node = await mount(renderNativeDetailsResult(action("get"), result(info)));
    expect(node.textContent).toContain("ContainersNotReady");
    expect(node.textContent).toContain("Image pulling");
    expect((await mount(renderNativeDetailsResult(action("create"), result(info)))).textContent).toContain("Pending");
    expect((await mount(renderNativeDetailsResult(action("list"), result({ sandboxes: [] })))).textContent).toContain(
      "No sandboxes."
    );
    expect(
      (await mount(renderNativeDetailsResult(action("list"), result({ sandboxes: [info] })))).textContent
    ).toContain("waiting-sandbox");
  });

  it.each([true, false])("distinguishes disposal existed=%s", async (existed) => {
    expect(
      (await mount(renderNativeDetailsResult(action("dispose"), result({ name: "box", existed })))).textContent
    ).toContain(existed ? "disposed" : "already absent");
  });

  it.each([
    [{ kind: "exited", exit_code: 2 }, "Exit 2"],
    [{ kind: "killed", signal: 9 }, "Killed by signal 9"],
    [{ kind: "timed_out" }, "Timed out"],
  ])("preserves execution outcomes and truncation", async (exit, label) => {
    const node = await mount(
      renderNativeDetailsResult(
        action("exec"),
        result({
          exit,
          stdout: { truncated_text: "", total_bytes: 123 },
          stderr: "error",
          duration_seconds: 1.5,
        })
      )
    );
    expect(node.textContent).toContain(label);
    expect(node.textContent).toContain("truncated by the server; 123 total bytes");
    expect(node.textContent).toContain("error");
  });

  it("renders pod argv with safe argument quoting and without implying shell execution", async () => {
    const node = await mount(
      renderPaneCollapsed(
        { group: "kubernetes_admin", name: "pods_exec" },
        { name: "pod", command: ["printf", "%s", "$HOME", "a'b", ""] }
      )
    );
    expect(node.querySelector(".agentplane-code-inline")?.textContent).toBe("printf '%s' '$HOME' 'a'\\''b' ''");
    expect(node.querySelector(".agentplane-tok-string")).toBeNull();

    const opened = await mount(
      renderPaneOpened(
        { group: "kubernetes_admin", name: "pods_exec" },
        { name: "pod", namespace: "apps", container: "worker", command: ["printf", "%s", "$HOME"] }
      )
    );
    expect(opened.textContent).not.toContain("Pod apps/pod");
    expect(opened.textContent).toContain("Container worker");
    expect(opened.querySelector(".agentplane-code-block .cm-line")?.textContent).toBe("printf '%s' '$HOME'");
    expect(opened.querySelector(".agentplane-tok-string")).toBeNull();
  });
});
