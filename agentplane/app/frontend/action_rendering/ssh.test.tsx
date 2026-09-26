// @vitest-environment happy-dom
import { act, type ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { mount } from "../actions_testing";
import { renderPreview } from "./entry";
import { renderResultPreview } from "./result_entry";
import { execArgumentsPreview, execResultPreview } from "./ssh";

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
  return [...container.querySelectorAll("pre")].map((block) => [
    block.previousElementSibling?.textContent ?? "",
    block.textContent ?? "",
  ]);
}

describe("ssh exec arguments", () => {
  it("shows the target as user@host and the command as text, never as markup", async () => {
    const command = 'echo "<b>test</b>" > /tmp/test-file && cat /tmp/test-file';
    const container = await drawn(renderPreview(execArgumentsPreview, { ...ARGUMENTS, command }));
    expect(container.textContent).toContain("test-user@test-host.example");
    expect(container.querySelector("pre")?.textContent).toBe(command);
    expect(container.querySelector("b")).toBeNull();
    expect(container.textContent).not.toContain("Timeout");
  });

  it("highlights the command as shell", async () => {
    // syntax_highlight.test.ts checks the whole command survives highlighting; happy-dom, which this
    // file runs in, drops the text ahead of the first token.
    const command = 'systemctl --user restart test-backup.service && echo "restarted at $(date -Is)"';
    const container = await drawn(renderPreview(execArgumentsPreview, { ...ARGUMENTS, command }));
    expect(container.querySelector("pre .hljs-string")?.textContent).toBe('"restarted at $(date -Is)"');
    expect(container.querySelector("pre .hljs-string .hljs-subst")?.textContent).toBe("$(date -Is)");
  });

  it("shows the timeout when the call sets one", async () => {
    const container = await drawn(renderPreview(execArgumentsPreview, { ...ARGUMENTS, timeout_seconds: 30 }));
    expect(container.textContent).toContain("Timeout 30 s");
  });

  it("takes no call it could not show whole", () => {
    expect(renderPreview(execArgumentsPreview, { ...ARGUMENTS, test_extra: "test-value" })).toBeNull();
    expect(renderPreview(execArgumentsPreview, { host: ARGUMENTS.host, user: ARGUMENTS.user })).toBeNull();
  });
});

describe("ssh exec result", () => {
  it("shows a success's exit code and output, leaving out an empty stream", async () => {
    const container = await drawn(renderResultPreview(execResultPreview, VALUE));
    const badge = container.querySelector(".mantine-Badge-root");
    expect(badge?.textContent).toBe("Exit 0");
    expect(badge?.getAttribute("style")).toContain("green");
    expect(streams(container)).toEqual([["stdout", "test-output"]]);
  });

  it("marks a failing exit code, and shows what the command wrote to stderr", async () => {
    const container = await drawn(
      renderResultPreview(execResultPreview, { ...VALUE, exit_code: 2, stderr: "test-error\n" })
    );
    const badge = container.querySelector(".mantine-Badge-root");
    expect(badge?.textContent).toBe("Exit 2");
    expect(badge?.getAttribute("style")).toContain("red");
    expect(streams(container)).toEqual([
      ["stdout", "test-output"],
      ["stderr", "test-error"],
    ]);
  });

  it("marks a stream the server truncated", async () => {
    const container = await drawn(renderResultPreview(execResultPreview, { ...VALUE, stdout_truncated: true }));
    expect(streams(container)).toEqual([["stdout · truncated by the server", "test-output"]]);
  });

  it("shows a long stream's first lines until asked for all of them", async () => {
    const lines = Array.from({ length: 45 }, (_, index) => `test-line-${index + 1}`);
    const container = await drawn(
      renderResultPreview(execResultPreview, { ...VALUE, stdout: `${lines.join("\n")}\n` })
    );
    expect(container.querySelector("pre")?.textContent).toBe(lines.slice(0, 20).join("\n"));
    const more = container.querySelector("button");
    expect(more?.textContent).toBe("Show all 45 lines");
    expect(more?.getAttribute("aria-expanded")).toBe("false");
    await act(async () => more?.click());
    expect(container.querySelector("pre")?.textContent).toBe(lines.join("\n"));
    expect(more?.textContent).toBe("Show the first 20 lines");
  });

  it("says when the command wrote nothing", async () => {
    const container = await drawn(renderResultPreview(execResultPreview, { ...VALUE, stdout: "" }));
    expect(streams(container)).toEqual([]);
    expect(container.textContent).toContain("No output.");
  });

  it("takes no value shaped otherwise", () => {
    expect(renderResultPreview(execResultPreview, { ...VALUE, test_extra: true })).toBeNull();
    expect(renderResultPreview(execResultPreview, { ...VALUE, exit_code: "test-not-a-number" })).toBeNull();
  });
});
