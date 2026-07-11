import { describe, expect, it } from "vitest";

import { kubectlToolPreview } from "./kubectl.tsx";

describe("kubectlToolPreview", () => {
  it("renders pods_delete for valid args", () => {
    const preview = kubectlToolPreview("pods_delete", { name: "my-pod", namespace: "default" });
    expect(preview).not.toBeNull();
    expect(preview).not.toBe(false);
  });

  it("returns null when args don't match the tool's schema", () => {
    expect(kubectlToolPreview("pods_delete", { namespace: "default" })).toBeNull(); // missing required name
  });

  it("returns null for a tool with no custom widget", () => {
    expect(kubectlToolPreview("pods_list", {})).toBeNull();
  });
});
