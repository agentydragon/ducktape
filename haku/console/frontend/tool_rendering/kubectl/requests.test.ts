import { describe, expect, it } from "vitest";

import { renderPreview } from "../entry.tsx";
import { kubectlPreviews } from "./requests.tsx";

describe("kubectlPreviews", () => {
  it("renders pods_delete for valid args", () => {
    expect(
      renderPreview(kubectlPreviews.pods_delete, { name: "my-pod", namespace: "default" }, "detailed")
    ).not.toBeNull();
  });

  it("renders pods_log target and options", () => {
    expect(
      renderPreview(
        kubectlPreviews.pods_log,
        { name: "api-0", namespace: "prod", container: "api", previous: true, tail: 50 },
        "detailed"
      )
    ).not.toBeNull();
  });

  it("renders resources_create_or_update in both variants (compact clamps the manifest)", () => {
    const manifest = Array.from({ length: 20 }, (_, i) => `line-${i}: value`).join("\n");
    for (const variant of ["compact", "detailed"] as const) {
      expect(renderPreview(kubectlPreviews.resources_create_or_update, { resource: manifest }, variant)).not.toBeNull();
    }
  });

  it("returns null when args don't match the tool's schema", () => {
    // missing required name
    expect(renderPreview(kubectlPreviews.pods_delete, { namespace: "default" }, "detailed")).toBeNull();
  });

  it("has no entry for a tool with no custom widget", () => {
    expect("pods_list" in kubectlPreviews).toBe(false);
  });
});
