import { expect, it } from "vitest";

import { validatesOffsetTimestamp } from "./external_reference_consumer";

it("loads a cross-package local ref and honors the explicit offset-date config", () => {
  expect(
    validatesOffsetTimestamp({
      id: 17,
      label: "from another package",
      created_at: "2026-10-27T09:30:00-07:00",
    })
  ).toBe(true);
  expect(
    validatesOffsetTimestamp({
      id: 17,
      label: "from another package",
      created_at: "2026-10-27T09:30:00",
    })
  ).toBe(false);
});
