import { ensureSyntaxTree } from "@codemirror/language";
import { EditorState } from "@codemirror/state";
import { json } from "@codemirror/lang-json";
import { yaml } from "@codemirror/lang-yaml";
import { describe, expect, it } from "vitest";

import { chooseCompactFolds } from "./code_block.tsx";

// Build a parsed state for `doc` and return the names of the top-level entries that compact mode
// would fold at `budget` visible lines. Each name comes from the fold range's header line (the text
// before ":"), so a fixture's expectation reads as "these top-level fields collapse."
function foldedEntryNames(doc: string, lang: "json" | "yaml", budget: number): string[] {
  const state = EditorState.create({ doc, extensions: [lang === "yaml" ? yaml() : json()] });
  ensureSyntaxTree(state, state.doc.length, 1000); // small fixtures parse synchronously
  return chooseCompactFolds(state, budget).map((f) =>
    state.doc.lineAt(f.from).text.split(":")[0].trim().replace(/^"|"$/g, "")
  );
}

describe("chooseCompactFolds", () => {
  it("folds a too-big middle sibling, keeping the leading and trailing entries visible", () => {
    const doc = [
      "first:",
      "  a: 1",
      "middle:",
      "  a: 1",
      "  b: 2",
      "  c: 3",
      "  d: 4",
      "  e: 5",
      "last:",
      "  z: 9",
    ].join("\n");
    // budget 5: `first` (2 lines) fits, `middle` (6) does not and folds, `last` is the final
    // sibling so it expands instead of folding.
    expect(foldedEntryNames(doc, "yaml", 5)).toEqual(["middle"]);
  });

  it("folds a verbose leading field rather than letting it starve a later field", () => {
    const doc = ["huge:", "  a: 1", "  b: 2", "  c: 3", "  d: 4", "  e: 5", "small:", "  x: 1"].join("\n");
    // The breadth cap reserves a header line for `small`, so `huge` folds even though it's first.
    expect(foldedEntryNames(doc, "yaml", 4)).toEqual(["huge"]);
  });

  it("does not fold when the payload already fits the budget", () => {
    const doc = ["a: 1", "b: 2", "c: 3"].join("\n");
    expect(foldedEntryNames(doc, "yaml", 10)).toEqual([]);
  });

  it("leaves nothing to fold in a flat object whose only container is the root", () => {
    const doc = ["{", '  "a": 1,', '  "b": 2,', '  "c": 3', "}"].join("\n");
    expect(foldedEntryNames(doc, "json", 2)).toEqual([]);
  });
});
