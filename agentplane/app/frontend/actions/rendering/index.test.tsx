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
    expect(details.textContent).toContain("List pods in namespace");
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

  it("uses SSH's custom pane and full-details argument renderers", async () => {
    const collapsed = await mount(renderPaneCollapsed(SSH_EXEC, SSH_EXEC_ARGUMENTS));
    const opened = await mount(renderPaneOpened(SSH_EXEC, SSH_EXEC_ARGUMENTS));
    const details = await mount(renderDetailsArguments(SSH_EXEC, SSH_EXEC_ARGUMENTS));

    expect(collapsed.textContent).toContain("$ echo test-output");
    expect(opened.textContent).toContain("test-user@test-host.example");
    expect(details.textContent).toContain("test-user@test-host.example");
  });

  it("leaves a slot to the host when no registered widget accepts it", () => {
    expect(renderPaneCollapsed(SSH_EXEC, { ...SSH_EXEC_ARGUMENTS, test_extra: true })).toBeNull();
    expect(renderPaneOpened({ group: "test_group", name: "exec" }, SSH_EXEC_ARGUMENTS)).toBeNull();
    expect(renderDetailsArguments(SSH_EXEC, { ...SSH_EXEC_ARGUMENTS, test_extra: true })).toBeNull();
    expect(renderDetailsArguments({ group: "__proto__", name: "constructor" }, {})).toBeNull();
  });

  it.each([
    ["resources_get", { apiVersion: "apps/v1", kind: "Deployment", name: "api" }, ["namespace: (not specified)"]],
    ["pods_log", { name: "api-0", tail: -1 }, ["namespace: (not specified)", "container: (not specified)"]],
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
    visible: ["apps/v1", "Deployment", "api", "namespace: (not specified)"],
  },
  {
    group: "kubernetes_admin",
    name: "pods_list_in_namespace",
    args: { namespace: "prod", fieldSelector: "status.phase=Running", labelSelector: "app=web" },
    visible: ["Get pods", "prod", "status.phase=Running", "app=web"],
  },
  {
    group: "kubernetes_admin",
    name: "resources_list",
    args: { apiVersion: "v1", kind: "Pod", fieldSelector: "status.phase=Running", labelSelector: "app=web" },
    visible: ["v1", "Pod", "all namespaces", "status.phase=Running", "app=web"],
  },
  {
    group: "kubernetes_admin",
    name: "pods_log",
    args: { name: "api-0", container: "sidecar", previous: true, tail: -1 },
    visible: ["api-0", "namespace: (not specified)", "sidecar", "previous: yes", "tail: -1"],
  },
  {
    group: "kubernetes_admin",
    name: "resources_delete",
    args: { apiVersion: "v1", kind: "Pod", name: "api-0", namespace: "prod", gracePeriodSeconds: 0 },
    visible: ["Delete resource", "v1", "Pod", "api-0", "prod", "0s"],
  },
  {
    group: "kubernetes_admin",
    name: "events_list",
    args: { namespace: "prod", fieldSelector: "type=Warning" },
    visible: ["List events", "prod", "type=Warning"],
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
