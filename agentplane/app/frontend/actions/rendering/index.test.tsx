// @vitest-environment happy-dom
import { describe, expect, it } from "vitest";

import { type CallToolResult, parseCallToolResult } from "../call_tool_result";
import { ACTION_PRESENTATION_CATALOG } from "../presentation_catalog";
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

  it("renders static Calendar event data and leaves calendar IDs visible", async () => {
    const action = { group: "google_calendar", name: "create_event" };
    const args = {
      summary: "Planning session",
      start: { date_time: "2026-10-12T09:00:00-07:00", time_zone: "America/Los_Angeles" },
      end: { date_time: "2026-10-12T10:00:00-07:00", time_zone: "America/Los_Angeles" },
      calendar_id: "team@group.calendar.google.com",
      attendees: ["reader@example.com"],
    };
    const label = await mount(renderActionLabel(action, args));
    const opened = await mount(renderPaneOpened(action, args));
    const details = await mount(renderDetailsArguments(action, args));

    expect(label.textContent).toContain("Create calendar event: Planning session");
    expect(opened.textContent).toContain("2026-10-12T09:00:00-07:00");
    expect(opened.textContent).not.toContain("Planning session");
    expect(details.textContent).toContain("team@group.calendar.google.com");
    expect(details.textContent).toContain("reader@example.com");
    expect(details.textContent).not.toContain("Planning session");

    const result = parseCallToolResult({
      content: [],
      structuredContent: {
        event_id: "event-123",
        summary: "Planning session",
        start: args.start,
        end: args.end,
        html_link: "https://calendar.google.com/calendar/event?eid=event-123",
      },
      isError: false,
    });
    if (result === null) throw new Error("the fixture is not a CallToolResult");
    const renderedResult = await mount(renderDetailsResult(action, result));
    expect(renderedResult.textContent).toContain("Planning session");
    expect(
      renderedResult.querySelector('a[href="https://calendar.google.com/calendar/event?eid=event-123"]')
    ).not.toBeNull();
  });

  it("renders Grocy list options and results without resolving IDs", async () => {
    for (const [name, args, expected] of [
      ["products_list", { detail: "full" }, "Full Product records"],
      ["quantity_units_list", {}, "Quantity unit names"],
      ["get_system_info", {}, "Grocy server version and system details"],
    ] as const) {
      const action = { group: "grocy_sf", name };
      const opened = await mount(renderPaneOpened(action, args));
      const details = await mount(renderDetailsArguments(action, args));
      expect(opened.textContent).toContain(expected);
      expect(details.textContent).toContain(expected);
    }

    const action = { group: "grocy_sf", name: "stock_add" };
    const result = parseCallToolResult({
      content: [],
      structuredContent: [
        {
          kind: "ok",
          product_name: "Tea",
          amount_delta: 2,
          new_amount: 4,
          qu_name: "box",
          location_name: "Pantry",
          best_before_date: "2027-01-01",
        },
        { kind: "error", error: "Unknown product" },
      ],
      isError: false,
    });
    if (result === null) throw new Error("the fixture is not a CallToolResult");
    const rendered = await mount(renderDetailsResult(action, result));
    expect(rendered.textContent).toContain("1 added");
    expect(rendered.textContent).toContain("Tea");
    expect(rendered.textContent).toContain("Unknown product");
    expect(rendered.textContent).toContain("2027-01-01");
  });

  it("renders Tana calendar arguments from submitted data without resolving the workspace", async () => {
    const action = { group: "tana", name: "get_or_create_calendar_node" };
    const args = { workspaceId: "workspace-123", granularity: "week", date: "2026-10-12" };
    const opened = await mount(renderPaneOpened(action, args));
    const details = await mount(renderDetailsArguments(action, args));

    for (const rendered of [opened, details]) {
      expect(rendered.textContent).toContain("week");
      expect(rendered.textContent).toContain("2026-10-12");
      expect(rendered.textContent).toContain("workspace-123");
    }
  });

  it("keeps lookup-backed Gmail and Grocy views on the submitted-data fallback", async () => {
    const gmail = await mount(
      renderPaneOpened({ group: "gmail", name: "threads_get" }, { id: "opaque-thread", format: "full" })
    );
    const grocy = await mount(
      renderPaneOpened({ group: "grocy_sf", name: "products_create" }, { items: [{ product_id: 42 }] })
    );

    expect(gmail.textContent).toContain("opaque-thread");
    expect(gmail.textContent).toContain("Format");
    expect(gmail.textContent).toContain("full");
    expect(grocy.textContent).toContain("Product");
    expect(grocy.textContent).toContain("42");
    expect(grocy.textContent).not.toContain("Could not load");
  });

  it("registers readable label, pane, and full-details views for every migrated action", async () => {
    const argumentsFor = (group: string, name: string): Record<string, unknown> => {
      if (group === "kubernetes_admin") {
        if (name === "pods_list_in_namespace") return { namespace: "demo", fieldSelector: "type=Warning" };
        if (name === "resources_get") return { apiVersion: "v1", kind: "Pod", name: "demo", namespace: "demo" };
        if (name === "resources_delete") {
          return { apiVersion: "v1", kind: "Pod", name: "demo", namespace: "demo", gracePeriodSeconds: 0 };
        }
        if (name === "resources_list")
          return { apiVersion: "v1", kind: "Pod", namespace: "demo", labelSelector: "app=demo" };
        if (name === "events_list") return { namespace: "demo", fieldSelector: "type=Warning" };
        if (name === "pods_log") return { name: "demo", namespace: "demo", container: "main", tail: 20 };
        if (name === "pods_exec")
          return { name: "demo", namespace: "demo", container: "main", command: ["echo", "ok"] };
        if (name === "pods_delete") return { name: "demo", namespace: "demo" };
        return { resource: "apiVersion: v1\nkind: ConfigMap" };
      }
      if (group === "github") {
        return {
          owner: "example",
          repo: "demo",
          title: "Update docs",
          head: "docs",
          base: "devel",
          body: "",
          draft: true,
          maintainer_can_modify: false,
          reviewers: ["reviewer"],
        };
      }
      if (group === "gmail" && name === "drafts_create") {
        return { to: ["reader@example.com"], subject: "Review", body: "Draft body" };
      }
      if (group === "gmail" && name === "threads_list") return { q: "from:alerts@example.com" };
      if (group === "google_calendar") {
        if (name === "create_event") {
          return {
            summary: "Planning",
            start: { date_time: "2026-10-12T09:00:00-07:00", time_zone: "America/Los_Angeles" },
            end: { date_time: "2026-10-12T10:00:00-07:00", time_zone: "America/Los_Angeles" },
          };
        }
        if (name === "update_event") return { event_id: "event-123", summary: "Planning" };
        if (name === "get_event" || name === "delete_event") return { event_id: "event-123" };
        if (name === "list_event_instances") return { recurring_event_id: "series-123" };
        return {};
      }
      if (group === "grocy_sf") {
        if (name === "products_list") return { detail: "brief" };
        if (name === "quantity_units_list") return { detail: "full" };
        if (name === "get_system_info") return {};
      }
      if (group === "tana" && name === "get_or_create_calendar_node") {
        return { workspaceId: "workspace", granularity: "day", date: "2026-10-12" };
      }
      if (group === "ssh" && name === "exec") return SSH_EXEC_ARGUMENTS as Record<string, unknown>;
      return { example: "value" };
    };

    const grocyResults: Record<string, { value: unknown; expected: string }> = {
      stock_add: {
        value: [
          { kind: "ok", product_name: "Tea", amount_delta: 2, new_amount: 4, qu_name: "box", location_name: "Pantry" },
        ],
        expected: "1 added",
      },
      stock_entry_edit: {
        value: [
          {
            kind: "ok",
            entry: { entry_id: 9, product_name: "Tea", amount: 4, qu_name: "box", location_name: "Pantry" },
            changes: {},
          },
        ],
        expected: "1 edited",
      },
      stock_get: {
        value: [{ product_name: "Tea", amount: 4, qu_name: "box", location_name: "Pantry" }],
        expected: "1 stock item",
      },
      products_list: { value: [{ id: 1, name: "Tea" }], expected: "1 found" },
      quantity_units_list: { value: [{ id: 1, name: "box" }], expected: "1 found" },
      get_system_info: { value: { php_version: "8.3" }, expected: "PHP version" },
      products_create: { value: [{ kind: "ok", created_object_id: 1 }], expected: "1 created" },
      shopping_list_get: { value: { name: "Groceries", items: [] }, expected: "Groceries" },
      shopping_list_items_add: {
        value: [{ kind: "ok", item_id: 2, product_name: "Tea", amount: 1, qu_name: "box" }],
        expected: "1 added",
      },
      shopping_list_items_remove: {
        value: [{ kind: "ok", item_id: 2, product_name: "Tea", amount: 1, qu_name: "box" }],
        expected: "1 removed",
      },
    };

    for (const { group, name, resultLabel } of ACTION_PRESENTATION_CATALOG) {
      const action = { group, name };
      const args = argumentsFor(group, name);
      const label = renderActionLabel(action, args);
      const opened = renderPaneOpened(action, args);
      const details = renderDetailsArguments(action, args);
      expect(label).not.toBeNull();
      expect(opened).not.toBeNull();
      expect(details).not.toBeNull();
      expect((await mount(label)).textContent).not.toBe("");
      if (resultLabel !== undefined) {
        const grocyResult = group === "grocy_sf" ? grocyResults[name] : undefined;
        const result = grocyResult
          ? parseCallToolResult({ content: [], structuredContent: grocyResult.value, isError: false })!
          : group === "ssh" && name === "exec"
            ? stored()
            : group === "google_calendar"
              ? parseCallToolResult({
                  content: [],
                  structuredContent:
                    name === "list_events" || name === "list_event_instances"
                      ? { events: [] }
                      : { event_id: "event-123", summary: "Planning" },
                  isError: false,
                })!
              : stored({ example: "value" });
        const expectedLabel =
          grocyResult?.expected ??
          (group === "ssh" && name === "exec"
            ? "Exit 0"
            : group === "gmail" && name === "threads_list"
              ? "No threads found"
              : group === "google_calendar"
                ? name === "list_events" || name === "list_event_instances"
                  ? "No events."
                  : "Planning"
                : resultLabel);
        expect((await mount(renderDetailsResult(action, result))).textContent).toContain(expectedLabel);
      }
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
