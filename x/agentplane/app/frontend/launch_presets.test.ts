import { describe, expect, it } from "vitest";

import { changedDefaults, effectiveThreadDefaults } from "./launch_presets";

const inherited = {
  provider: "codex" as const,
  model: "preset-model",
  reasoning_effort: "medium",
  instructions: "preset instructions",
};

describe("launch preset editing", () => {
  it("stores only explicit edits so untouched preset fields stay live", () => {
    expect(changedDefaults({ ...inherited, model: "edited-model" }, inherited)).toEqual({ model: "edited-model" });
    expect(changedDefaults(inherited, inherited)).toBeUndefined();
  });

  it("lets explicit values replace inherited values, including clearing instructions", () => {
    expect(effectiveThreadDefaults(inherited, { model: "edited-model", instructions: "" })).toEqual({
      ...inherited,
      model: "edited-model",
      instructions: "",
    });
  });
});
