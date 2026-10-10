// @vitest-environment happy-dom
import { describe, expect, it } from "vitest";

import { type CallToolResult, parseCallToolResult } from "../call_tool_result";
import { mount, SSH_EXEC_ARGUMENTS, sshExec } from "../testing";
import { canApproveInline, compactActionArguments, renderArguments, renderMcpResult } from "./index";

const SSH_EXEC = { group: "ssh", name: "exec" };

/** The CallToolResult stored for an ssh `exec` call whose `ExecResult` has `returned`'s fields. */
function stored(returned: Record<string, unknown> = {}): CallToolResult {
  const result = parseCallToolResult(sshExec("succeeded", returned).execution?.result);
  if (result === null) throw new Error("the fixture is not a CallToolResult");
  return result;
}

describe("renderArguments", () => {
  it("draws a registered Action's arguments with its widget", async () => {
    const container = await mount(renderArguments(SSH_EXEC, SSH_EXEC_ARGUMENTS));
    expect(container.textContent).toContain("test-user@test-host.example");
  });

  it("leaves arguments to their JSON when no widget takes them", () => {
    // An argument the widget does not draw, another group's tool of the same name, and names that
    // are members of every object.
    expect(renderArguments(SSH_EXEC, { ...SSH_EXEC_ARGUMENTS, test_extra: true })).toBeNull();
    expect(renderArguments({ group: "test_group", name: "exec" }, SSH_EXEC_ARGUMENTS)).toBeNull();
    expect(renderArguments({ group: "__proto__", name: "constructor" }, SSH_EXEC_ARGUMENTS)).toBeNull();
  });

  it.each([
    ["resources_get", { apiVersion: "apps/v1", kind: "Deployment", name: "api" }, ["namespace: (not specified)"]],
    ["pods_log", { name: "api-0", tail: -1 }, ["namespace: (not specified)", "container: (not specified)"]],
  ] as const)(
    "uses the Kubernetes argument widget for %s when optional fields are omitted",
    async (name, args, visible) => {
      const container = await mount(renderArguments({ group: "kubernetes_admin", name }, args));
      for (const value of visible) expect(container.textContent).toContain(value);
    }
  );
});

describe("renderMcpResult", () => {
  it("draws the tool's value with the Action's widget", async () => {
    const container = await mount(renderMcpResult(SSH_EXEC, stored()));
    expect(container.textContent).toContain("Exit 0");
    expect(container.textContent).not.toContain("Structured content");
  });

  it("draws the result as the tool answered when the widget does not take its value", async () => {
    const container = await mount(renderMcpResult(SSH_EXEC, stored({ exit_code: "test-not-a-number" })));
    expect(container.textContent).toContain("Structured content");
    expect(container.textContent).toContain('"exit_code": "test-not-a-number"');
    expect(container.textContent).not.toContain("Exit");
  });

  it("draws an error result as the tool answered", async () => {
    const failure = { kind: "target_not_configured", message: "SSH host/user target is not configured" };
    const result = parseCallToolResult({ content: [{ type: "text", text: JSON.stringify(failure) }], isError: true });
    if (result === null) throw new Error("the fixture is not a CallToolResult");
    const container = await mount(renderMcpResult(SSH_EXEC, result));
    expect(container.textContent).toContain("Tool error");
    expect(container.textContent).toContain('"kind": "target_not_configured"');
  });

  it("draws an unregistered Action's result as the tool answered", async () => {
    const container = await mount(renderMcpResult({ group: "test_group", name: "exec" }, stored()));
    expect(container.textContent).toContain("Structured content");
    expect(container.textContent).not.toContain("Exit 0");
  });
});

const compactCases: Array<{ group: string; name: string; args: Record<string, unknown>; visible: string[] }> = [
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

describe("compact rendering and inline approval", () => {
  it.each(compactCases)("shows all arguments of $group/$name", async ({ group, name, args, visible }) => {
    expect(canApproveInline({ group, name }, args)).toBe(true);
    const widget = compactActionArguments({ group, name }, args);
    expect(widget).not.toBeNull();
    const container = await mount(widget);
    for (const value of visible) expect(container.textContent).toContain(value);
  });

  it.each(compactCases)("fails closed for extra arguments on $group/$name", ({ group, name, args }) => {
    expect(canApproveInline({ group, name }, { ...args, invisible: "must review" })).toBe(false);
    expect(compactActionArguments({ group, name }, { ...args, invisible: "must review" })).toBeNull();
  });

  it("requires expanded review for PR descriptions or unknown Action identities", () => {
    const pr = compactCases.find(
      (candidate) => candidate.group === "github" && candidate.name === "create_pull_request"
    );
    if (pr === undefined) throw new Error("missing pull request compact fixture");
    expect(canApproveInline({ group: pr.group, name: pr.name }, { ...pr.args, body: "important text" })).toBe(false);
    expect(
      compactActionArguments({ group: pr.group, name: pr.name }, { ...pr.args, body: "important text" })
    ).not.toBeNull();
    expect(
      canApproveInline(
        { group: "kubernetes_admin", name: "resources_delete" },
        {
          apiVersion: "v1",
          kind: "Pod",
          name: "api-0",
        }
      )
    ).toBe(false); // The backend's configured namespace would otherwise be hidden.
    expect(canApproveInline({ group: pr.group, name: pr.name }, { ...pr.args, title: "x".repeat(121) })).toBe(false);
    expect(
      canApproveInline({ group: pr.group, name: pr.name }, { ...pr.args, reviewers: Array(5).fill("reviewer") })
    ).toBe(false);
    expect(canApproveInline({ group: "ssh", name: "exec" }, SSH_EXEC_ARGUMENTS)).toBe(false);
    expect(canApproveInline({ group: "__proto__", name: "constructor" }, {})).toBe(false);
  });
});
