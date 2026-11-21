// @vitest-environment happy-dom
import { create } from "@bufbuild/protobuf";
import { describe, expect, it } from "vitest";

import { ActionIdentitySchema } from "../../../../../action_service/api/contracts/action_service_pb";
import { CallToolResultView, type CallToolResult } from "../../call_tool_result";
import { mount, toolResult } from "../../testing/harness";
import { renderDetailsResult } from "../index";

const PODS_EXEC = create(ActionIdentitySchema, { group: "kubernetes_admin", name: "pods_exec" });

/** The Pod exec result as the host draws it: the Action's own view, else the generic envelope view. */
async function drawDetailsResult(result: CallToolResult) {
  const custom = renderDetailsResult(PODS_EXEC, result);
  return mount(custom === undefined ? <CallToolResultView result={result} /> : custom);
}

describe("Pod exec result presentation", () => {
  it("uses the shared output view for one plain text block and preserves its final newline", async () => {
    const view = await drawDetailsResult(
      toolResult({ content: [{ type: "text", text: "row one\nrow two\n" }], isError: false })
    );
    expect(view.querySelector(".agentplane-output-label")?.textContent).toContain("Output · 2 lines");
    expect([...view.querySelectorAll(".ui-code-block .cm-line")].map((line) => line.textContent)).toEqual([
      "row one",
      "row two",
      "",
    ]);
    expect(view.textContent).not.toMatch(/stderr|exit code/i);
  });

  it("keeps distinct content in the generic result view", async () => {
    const view = await drawDetailsResult(
      toolResult({
        content: [
          { type: "text", text: "first stream" },
          { type: "text", text: "separate diagnostic" },
        ],
        isError: false,
      })
    );
    expect(view.querySelector(".agentplane-output-disclosure")).toBeNull();
    expect(view.textContent).toContain("first stream");
    expect(view.textContent).toContain("separate diagnostic");
  });

  it("keeps a structured result generic when another block adds distinct content", async () => {
    const view = await drawDetailsResult(
      toolResult({
        structuredContent: "first stream",
        content: [
          { type: "text", text: "first stream" },
          { type: "text", text: "separate diagnostic" },
        ],
        isError: false,
      })
    );
    expect(view.querySelector(".agentplane-output-disclosure")).toBeNull();
    expect(view.textContent).toContain("separate diagnostic");
  });
});
