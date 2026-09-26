// @vitest-environment happy-dom
import { describe, expect, it } from "vitest";

import { type CallToolResult, parseCallToolResult } from "../call_tool_result";
import { mount, SSH_EXEC_ARGUMENTS, sshExec } from "../testing";
import { renderArguments, renderMcpResult } from "./index";

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
