// @vitest-environment happy-dom
import { describe, expect, it } from "vitest";

import { type CallToolResult, parseCallToolResult } from "../call_tool_result";
import { mount, SSH_EXEC_ARGUMENTS, sshExec } from "../testing";
import {
  canApproveInline,
  renderActionLabel,
  renderDetailsArguments,
  renderDetailsResult,
  renderPaneCollapsed,
  renderPaneOpened,
  shouldRenderPaneRequestTitle,
} from "./index";

const SSH_EXEC = { group: "ssh", name: "exec" };

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

    expect(collapsed.textContent).toContain("Filters");
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
    expect(renderActionLabel(action, { namespace: "tofu-controller", hidden: true })).toBeNull();
    expect(shouldRenderPaneRequestTitle(action, args, "List pods in namespace tofu-controller")).toBe(false);
    expect(shouldRenderPaneRequestTitle(action, args, "Inspect the running demo pods")).toBe(true);
  });

  it.each([
    ["resources_get", { apiVersion: "v1", kind: "Pod", name: "web-0", namespace: "apps" }, "Get Pod apps/web-0"],
    ["resources_delete", { apiVersion: "v1", kind: "Pod", name: "web-0", namespace: "apps" }, "Delete Pod apps/web-0"],
    ["pods_delete", { name: "web-0", namespace: "apps" }, "Delete Pod apps/web-0"],
    ["pods_exec", { name: "web-0", namespace: "apps", command: ["true"] }, "Run command in Pod apps/web-0"],
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
    expect(opened.textContent).toContain("API version: apps/v1");
    expect(details.textContent).toContain("API version: apps/v1");
    expect(details.textContent).not.toContain("Get Deployment");
    expect(details.textContent).not.toContain("apps/web");
  });

  it("puts list scope in Kubernetes action labels and leaves only filters in the pane", async () => {
    const listArgs = { apiVersion: "v1", kind: "Pod", namespace: "apps", labelSelector: "app=web" };
    const listLabel = await mount(renderActionLabel({ group: "kubernetes_admin", name: "resources_list" }, listArgs));
    const listPane = await mount(renderPaneOpened({ group: "kubernetes_admin", name: "resources_list" }, listArgs));
    expect(listLabel.textContent).toContain("List Pod resources in namespace apps");
    expect(listPane.textContent).toContain("label selector: app=web");
    expect(listPane.textContent).not.toContain("apps");

    const eventsArgs = { namespace: "apps", fieldSelector: "type=Warning" };
    const eventsLabel = await mount(renderActionLabel({ group: "kubernetes_admin", name: "events_list" }, eventsArgs));
    const eventsPane = await mount(renderPaneOpened({ group: "kubernetes_admin", name: "events_list" }, eventsArgs));
    expect(eventsLabel.textContent).toContain("List events in namespace apps");
    expect(eventsPane.textContent).toContain("type=Warning");
    expect(eventsPane.textContent).not.toContain("apps");
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

    expect(label.textContent).toContain("Draft email: Release notes");
    expect(collapsed.textContent).toContain("reader@example.com");
    expect(opened.textContent).toContain("copy@example.com");
    expect(opened.textContent).toContain("The release is ready.");
    expect(details.textContent).toContain("Please review it.");
    expect(details.textContent).not.toContain("Release notes");

    const result = parseCallToolResult({ content: [], structuredContent: { id: "draft-123" }, isError: false });
    if (result === null) throw new Error("the fixture is not a CallToolResult");
    const renderedResult = await mount(renderDetailsResult(action, result));
    expect(renderedResult.textContent).toContain("Draft created");
    expect(
      renderedResult.querySelector('a[href="https://mail.google.com/mail/u/0/#drafts?compose=draft-123"]')
    ).not.toBeNull();
  });

  it("renders Gmail search arguments and results without fetching subjects", async () => {
    const action = { group: "gmail", name: "threads_list" };
    const args = { q: "from:alerts@example.com", maxResults: 5, includeSpamTrash: false };
    const label = await mount(renderActionLabel(action, args));
    const collapsed = await mount(renderPaneCollapsed(action, args));
    const opened = await mount(renderPaneOpened(action, args));

    expect(label.textContent).toContain("Search Gmail threads");
    expect(collapsed.textContent).toContain("from:alerts@example.com");
    expect(opened.textContent).toContain("Maximum results: 5");
    expect(opened.textContent).toContain("Include spam and trash");
    expect(opened.textContent).toContain("false");

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
      ["get_system_info", {}, "No arguments."],
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
      expect(renderActionLabel(action, { id: "opaque-id" })).toBeNull();
      expect(renderPaneCollapsed(action, { id: "opaque-id" })).toBeNull();
      expect(renderPaneOpened(action, { id: "opaque-id" })).toBeNull();
      expect(renderDetailsArguments(action, { id: "opaque-id" })).toBeNull();

      const raw = await mount(renderDetailsResult(action, result));
      expect(raw.textContent).toContain("Structured content");
      expect(raw.textContent).toContain('"lookup_dependent_value": "preserve raw response"');
    }
  });

  it("uses an Action-owned collapsed view instead of synthesizing a summary from fields", async () => {
    const collapsed = await mount(
      renderPaneCollapsed(
        { group: "gmail", name: "drafts_create" },
        { to: ["user@example.com"], subject: "Deploy", body: "Ready" }
      )
    );
    expect(collapsed.textContent).toContain("To user@example.com");
    expect(collapsed.textContent).not.toContain("Deploy");
  });

  it("leaves a slot to the host when no registered widget accepts it", () => {
    expect(renderPaneCollapsed(SSH_EXEC, { ...SSH_EXEC_ARGUMENTS, test_extra: true })).toBeNull();
    expect(renderPaneOpened({ group: "test_group", name: "exec" }, SSH_EXEC_ARGUMENTS)).toBeNull();
    expect(renderDetailsArguments(SSH_EXEC, { ...SSH_EXEC_ARGUMENTS, test_extra: true })).toBeNull();
    expect(renderDetailsArguments({ group: "__proto__", name: "constructor" }, {})).toBeNull();
  });

  it.each([
    ["resources_get", { apiVersion: "apps/v1", kind: "Deployment", name: "api" }, ["API version: apps/v1"]],
    ["pods_log", { name: "api-0", tail: -1 }, ["container: (default)", "previous: no", "tail: -1"]],
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
    const container = await mount(renderDetailsResult(SSH_EXEC, stored({ exit_code: "test-not-a-number" })));
    expect(container.textContent).toContain("Structured content");
    expect(container.textContent).toContain('"exit_code": "test-not-a-number"');
    expect(container.textContent).not.toContain("Exit");
  });

  it("draws an error result as the tool answered", async () => {
    const failure = { kind: "target_not_configured", message: "SSH host/user target is not configured" };
    const result = parseCallToolResult({ content: [{ type: "text", text: JSON.stringify(failure) }], isError: true });
    if (result === null) throw new Error("the fixture is not a CallToolResult");
    const container = await mount(renderDetailsResult(SSH_EXEC, result));
    expect(container.textContent).toContain("Tool error");
    expect(container.textContent).toContain('"kind": "target_not_configured"');
  });

  it("draws an unregistered Action's result as the tool answered", async () => {
    const container = await mount(renderDetailsResult({ group: "test_group", name: "exec" }, stored()));
    expect(container.textContent).toContain("Structured content");
    expect(container.textContent).not.toContain("Exit 0");
  });
});

const quickApprovalCases: Array<{ group: string; name: string; args: Record<string, unknown>; visible: string[] }> = [
  {
    group: "kubernetes_admin",
    name: "resources_get",
    args: { apiVersion: "apps/v1", kind: "Deployment", name: "api" },
    visible: ["API version: apps/v1"],
  },
  {
    group: "kubernetes_admin",
    name: "pods_list_in_namespace",
    args: { namespace: "prod", fieldSelector: "status.phase=Running", labelSelector: "app=web" },
    visible: ["Filters", "status.phase=Running", "app=web"],
  },
  {
    group: "kubernetes_admin",
    name: "resources_list",
    args: { apiVersion: "v1", kind: "Pod", fieldSelector: "status.phase=Running", labelSelector: "app=web" },
    visible: ["API version: v1", "status.phase=Running", "app=web"],
  },
  {
    group: "kubernetes_admin",
    name: "pods_log",
    args: { name: "api-0", container: "sidecar", previous: true, tail: -1 },
    visible: ["sidecar", "previous: yes", "tail: -1"],
  },
  {
    group: "kubernetes_admin",
    name: "resources_delete",
    args: { apiVersion: "v1", kind: "Pod", name: "api-0", namespace: "prod", gracePeriodSeconds: 0 },
    visible: ["grace period: 0s"],
  },
  {
    group: "kubernetes_admin",
    name: "events_list",
    args: { namespace: "prod", fieldSelector: "type=Warning" },
    visible: ["field selector", "type=Warning"],
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
      "feature → devel",
      "description: empty",
      "draft: yes",
      "maintainer edits: no",
      "reviewer1",
    ],
  },
];

describe("opened-pane renderers", () => {
  it.each(quickApprovalCases.filter(({ name }) => name !== "pods_list_in_namespace"))(
    "draws all displayed request fields for $group/$name",
    async ({ group, name, args, visible }) => {
      const container = await mount(renderPaneOpened({ group, name }, args));
      for (const value of visible) expect(container.textContent).toContain(value);
    }
  );
});

describe("quick approval capability", () => {
  it.each(quickApprovalCases)("validates the whole $group/$name request independently", ({ group, name, args }) => {
    expect(canApproveInline({ group, name }, args)).toBe(true);
    expect(canApproveInline({ group, name }, { ...args, invisible: "must review" })).toBe(false);
  });

  it("requires full review for PR descriptions, hidden defaults, and unknown actions", () => {
    const pr = quickApprovalCases.find((candidate) => candidate.group === "github");
    if (pr === undefined) throw new Error("missing pull request fixture");
    expect(canApproveInline({ group: pr.group, name: pr.name }, { ...pr.args, body: "important text" })).toBe(false);
    expect(
      canApproveInline(
        { group: "kubernetes_admin", name: "resources_delete" },
        { apiVersion: "v1", kind: "Pod", name: "api-0" }
      )
    ).toBe(false); // The backend's configured namespace would otherwise be hidden.
    expect(canApproveInline({ group: pr.group, name: pr.name }, { ...pr.args, title: "x".repeat(121) })).toBe(false);
    expect(
      canApproveInline({ group: pr.group, name: pr.name }, { ...pr.args, reviewers: Array(5).fill("reviewer") })
    ).toBe(false);
    expect(canApproveInline(SSH_EXEC, SSH_EXEC_ARGUMENTS)).toBe(false);
    expect(canApproveInline({ group: "__proto__", name: "constructor" }, {})).toBe(false);
  });

  it("still renders a long PR description for review while withholding quick approval", async () => {
    const pr = quickApprovalCases.find((candidate) => candidate.group === "github");
    if (pr === undefined) throw new Error("missing pull request fixture");
    const args = { ...pr.args, body: "important text" };

    expect(canApproveInline({ group: pr.group, name: pr.name }, args)).toBe(false);
    const container = await mount(renderPaneOpened({ group: pr.group, name: pr.name }, args));
    expect(container.textContent).toContain("open Review");
  });
});
