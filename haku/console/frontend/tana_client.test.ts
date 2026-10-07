import { describe, expect, it } from "vitest";

import { nodeNameFromMarkdown } from "./tana_client";

describe("nodeNameFromMarkdown", () => {
  it("extracts the matching Tana node name", () => {
    const markdown = "- [ ] First node <!-- node-id: other -->\n- Target node #Task <!-- node-id: target -->";

    expect(nodeNameFromMarkdown(markdown, "target")).toBe("Target node #Task");
  });

  it("returns null when the requested marker or its name is absent", () => {
    expect(nodeNameFromMarkdown("- A node <!-- node-id: other -->", "target")).toBeNull();
    expect(nodeNameFromMarkdown("- [x] <!-- node-id: target -->", "target")).toBeNull();
  });
});
