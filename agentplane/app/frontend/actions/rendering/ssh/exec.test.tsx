// @vitest-environment happy-dom
import { act, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import producerFixture from "../../../../../../ssh_mcp_server/exec_payload_fixture.json";
import { mount } from "../../testing/harness";
import { renderPreview } from "../entry";
import { renderResultPreview } from "../result_entry";
import { execArgumentsPreview, execCollapsedPreview, execResultPreview } from "./exec";

const ARGUMENTS = { host: "test-host.example", user: "test-user", command: "echo test-output" };
const VALUE = {
  host: "test-host.example",
  user: "test-user",
  exit_code: 0,
  stdout: "test-output\n",
  stderr: "",
  stdout_truncated: false,
  stderr_truncated: false,
};

async function drawn(node: ReactNode | null): Promise<HTMLDivElement> {
  if (node === null) throw new Error("the widget did not take the fixture");
  return mount(node);
}

/** The labels of the output streams shown, in order, with each one's text. */
function streams(container: HTMLElement): Array<[string, string]> {
  return [...container.querySelectorAll(".ui-code-block-block")].map((block) => [
    block.closest(".agentplane-output-disclosure")?.querySelector(".agentplane-output-label")?.textContent?.trim() ??
      "",
    [...block.querySelectorAll(".cm-line")].map((line) => line.textContent ?? "").join("\n"),
  ]);
}

afterEach(() => vi.restoreAllMocks());

describe("SSH producer payload", () => {
  it("renders the registered FastMCP tool's serialized call and result", async () => {
    const argumentsContainer = await drawn(renderPreview(execArgumentsPreview, producerFixture.arguments));
    expect(argumentsContainer.textContent).toContain("printf producer-fixture");

    const resultContainer = await drawn(renderResultPreview(execResultPreview, producerFixture.result));
    expect(resultContainer.querySelector(".mantine-Badge-root")?.textContent).toBe("Exit 0");
    expect(streams(resultContainer)).toEqual([["stdout · 1 line", "producer-fixture"]]);
  });
});

describe("ssh exec arguments", () => {
  it("shows the command as text, never as markup, leaving the target to the label", async () => {
    const command = 'echo "<b>test</b>" > /tmp/test-file && cat /tmp/test-file';
    const container = await drawn(renderPreview(execArgumentsPreview, { ...ARGUMENTS, command }));
    expect(container.textContent).not.toContain("test-host.example");
    expect(streams(container)[0][1]).toBe(command);
    expect(container.querySelector("b")).toBeNull();
    expect(container.textContent).not.toContain("Timeout");
  });

  it("shows the timeout when the call sets one", async () => {
    const container = await drawn(renderPreview(execArgumentsPreview, { ...ARGUMENTS, timeout_seconds: 30 }));
    expect(container.textContent).toContain("Timeout 30 s");
  });

  it("takes no call it could not show whole", () => {
    expect(renderPreview(execArgumentsPreview, { ...ARGUMENTS, test_extra: "test-value" })).toBeUndefined();
    expect(renderPreview(execArgumentsPreview, { host: ARGUMENTS.host, user: ARGUMENTS.user })).toBeUndefined();
  });
});

describe("ssh exec summary", () => {
  it("shows the command as highlighted shell", async () => {
    const command = "if true; then echo test-output; fi";
    const container = await drawn(renderPreview(execCollapsedPreview, { ...ARGUMENTS, command }));
    expect(container.textContent).toContain(command);
    expect([...container.querySelectorAll(".ui-tok-keyword")].map((node) => node.textContent)).toEqual(
      expect.arrayContaining(["if", "then", "fi"])
    );
  });
});

describe("ssh exec result", () => {
  it("shows a success's exit code and output, leaving out an empty stream", async () => {
    const container = await drawn(renderResultPreview(execResultPreview, VALUE));
    const badge = container.querySelector(".mantine-Badge-root");
    expect(badge?.textContent).toBe("Exit 0");
    expect(badge?.getAttribute("style")).toContain("green");
    expect(streams(container)).toEqual([["stdout · 1 line", "test-output"]]);
  });

  it("marks a failing exit code, and shows what the command wrote to stderr", async () => {
    const container = await drawn(
      renderResultPreview(execResultPreview, { ...VALUE, exit_code: 2, stderr: "test-error\n" })
    );
    const badge = container.querySelector(".mantine-Badge-root");
    expect(badge?.textContent).toBe("Exit 2");
    expect(badge?.getAttribute("style")).toContain("red");
    expect(streams(container)).toEqual([
      ["stdout · 1 line", "test-output"],
      ["stderr · 1 line", "test-error"],
    ]);
  });

  it("marks a stream the server truncated", async () => {
    const container = await drawn(renderResultPreview(execResultPreview, { ...VALUE, stdout_truncated: true }));
    expect(streams(container)).toEqual([["stdout · 1 line · truncated by the server", "test-output"]]);
  });

  it("clips a stream taller than its cap behind a button that shows all of it", async () => {
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(10_000);
    const container = await drawn(renderResultPreview(execResultPreview, VALUE));
    const showAll = [...container.querySelectorAll("button")].find((button) => button.textContent === "Show all");
    expect(container.querySelector('[data-clamped="true"]')).not.toBeNull();
    await act(async () => showAll?.click());
    expect(container.querySelector('[data-clamped="true"]')).toBeNull();
    const outputDisclosure = container.querySelector<HTMLButtonElement>(
      ".agentplane-output-disclosure .ui-disclosure-summary"
    );
    expect(outputDisclosure?.getAttribute("aria-expanded")).toBe("true");
    await act(async () => outputDisclosure?.click());
    expect(outputDisclosure?.getAttribute("aria-expanded")).toBe("false");
    await act(async () => outputDisclosure?.click());
    expect(outputDisclosure?.getAttribute("aria-expanded")).toBe("true");
  });

  it("says how many lines a clipped stream has", async () => {
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(10_000);
    const lines = Array.from({ length: 45 }, (_, index) => `test-line-${index + 1}`);
    const container = await drawn(
      renderResultPreview(execResultPreview, { ...VALUE, stdout: `${lines.join("\n")}\n` })
    );
    expect([...container.querySelectorAll("button")].map((button) => button.textContent)).toContain(
      "Show all 45 lines"
    );
  });

  it("says when the command wrote nothing", async () => {
    const container = await drawn(renderResultPreview(execResultPreview, { ...VALUE, stdout: "" }));
    expect(streams(container)).toEqual([]);
    expect(container.textContent).toContain("No output.");
  });

  it("takes no value shaped otherwise", () => {
    expect(renderResultPreview(execResultPreview, { ...VALUE, test_extra: true })).toBeUndefined();
    expect(renderResultPreview(execResultPreview, { ...VALUE, exit_code: "test-not-a-number" })).toBeUndefined();
  });
});
