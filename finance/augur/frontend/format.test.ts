import { test, expect } from "vitest";

import { fmtNumber, fmtQuantity } from "./lib/format";

test("fmtQuantity preserves fractional crypto-sized positions", () => {
  expect(fmtQuantity(2.46761356)).toBe("2.46761356");
  expect(fmtQuantity(43.31454407)).toBe("43.31454407");
});

test("fmtQuantity still renders whole share counts compactly", () => {
  expect(fmtQuantity(23553)).toBe("23,553");
  expect(fmtQuantity(1500.0)).toBe("1,500");
});

test("fmtNumber remains integer-oriented for counts and coarse quantities", () => {
  expect(fmtNumber(2.46761356)).toBe("2");
});
