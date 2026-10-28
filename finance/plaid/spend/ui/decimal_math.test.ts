import { expect, it } from "vitest";

import { annualFundingMinorUnits, monthlySpendingMinorUnits, percentagePointDifference } from "./decimal_math";

it("keeps rate-derived minor-unit amounts exact before rounding", () => {
  // Number(rate) rounds this value up to the half-unit boundary. The exact product is below it.
  expect(annualFundingMinorUnits(4_503_599_627_370_496, "0.50000000000000008").toString()).toBe("2251799813685248");
  expect(monthlySpendingMinorUnits(12_000_000, "0.25", "0.2")?.toString()).toBe("200000");
});

it("rounds percentage-point comparisons from exact decimal rates", () => {
  // A binary float rounds the current rate up to 0.0005 and would show 0.1 percentage points.
  expect(percentagePointDifference("0.000499999999999999999999", "0").toFixed(1)).toBe("0.0");
});
