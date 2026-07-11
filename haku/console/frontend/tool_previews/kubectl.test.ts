import { describe, expect, it } from "vitest";

import { kubectlToolPreview } from "./kubectl.tsx";

describe("kubectlToolPreview", () => {
  it("renders pods_delete for valid args", () => {
    const preview = kubectlToolPreview("pods_delete", { name: "my-pod", namespace: "default" }, "detailed");
    expect(preview).not.toBeNull();
    expect(preview).not.toBe(false);
  });

  it("renders resources_create_or_update in both variants (compact clamps the manifest)", () => {
    const manifest = Array.from({ length: 20 }, (_, i) => `line-${i}: value`).join("\n");
    for (const variant of ["compact", "detailed"] as const) {
      expect(kubectlToolPreview("resources_create_or_update", { resource: manifest }, variant)).not.toBeNull();
    }
  });

  it("returns null when args don't match the tool's schema", () => {
    expect(kubectlToolPreview("pods_delete", { namespace: "default" }, "detailed")).toBeNull(); // missing required name
  });

  it("returns null for a tool with no custom widget", () => {
    expect(kubectlToolPreview("pods_list", {}, "detailed")).toBeNull();
  });
});
