import { describe, expect, it } from "vitest";

import { renderPreview } from "../entry.tsx";
import { tanaPreviews } from "./requests.tsx";

describe("tanaPreviews", () => {
  it("renders each focused tool in both variants", () => {
    const cases: [keyof typeof tanaPreviews, Record<string, unknown>][] = [
      ["import_tana_paste", { parentNodeId: "parent", content: "- One\n  - Two" }],
      ["get_or_create_calendar_node", { workspaceId: "workspace", granularity: "day", date: "2026-07-11" }],
      ["trash_node", { nodeId: "node" }],
      ["edit_node", { nodeId: "node", name: { old_string: "Old", new_string: "New" } }],
      ["move_node", { nodeId: "node", targetNodeId: "target", position: "end", keepSourceReference: false }],
    ];
    for (const [tool, args] of cases) {
      for (const variant of ["compact", "detailed"] as const) {
        expect(renderPreview(tanaPreviews[tool], args, variant)).not.toBeNull();
      }
    }
  });

  it("rejects malformed tool arguments", () => {
    expect(renderPreview(tanaPreviews.move_node, { nodeId: "node" }, "detailed")).toBeNull();
    expect(renderPreview(tanaPreviews.edit_node, { nodeId: "node" }, "detailed")).toBeNull();
  });

  it("leaves unimplemented Tana tools unregistered", () => {
    expect("tag" in tanaPreviews).toBe(false);
  });
});
