import { expect, it } from "vitest";

import { validateSearchArguments } from "./standalone_runtime";

it("validates the retained Pydantic JSON Schema artifact", () => {
  expect(
    validateSearchArguments({
      query: "invoice",
      required_note: null,
      filters: [{ field: "status", contains: "paid" }],
      labels: { owner: "finance" },
    })
  ).toBe(true);
  expect(
    validateSearchArguments({
      query: "invoice",
      filters: [],
      labels: {},
    })
  ).toBe(false);
  expect(
    validateSearchArguments({
      query: "invoice",
      required_note: "",
      filters: [],
      labels: {},
      unknown: true,
    })
  ).toBe(false);
});
