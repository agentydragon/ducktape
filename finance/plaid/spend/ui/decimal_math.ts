import Decimal from "decimal.js";

// Retain long server-provided decimal rates before rounding derived integer minor units.
Decimal.set({ precision: 80 });

export function percentagePointDifference(currentRate: string, referenceRate: string): Decimal {
  return (
    new Decimal(currentRate)
      .minus(referenceRate)
      .mul(100)
      // Match the prior Math.round tie rule (toward +infinity), including negative deltas.
      .toDecimalPlaces(1, Decimal.ROUND_HALF_CEIL)
  );
}

export function monthlySpendingMinorUnits(
  portfolioMinorUnits: number,
  drawRate: string,
  effectiveTaxRate: string | null
): Decimal | null {
  if (portfolioMinorUnits <= 0 || effectiveTaxRate == null) return null;
  const afterTaxRate = new Decimal(drawRate).mul(new Decimal(1).minus(effectiveTaxRate));
  return new Decimal(portfolioMinorUnits).mul(afterTaxRate).div(12).toDecimalPlaces(0, Decimal.ROUND_HALF_UP);
}

export function annualFundingMinorUnits(portfolioMinorUnits: number, drawRate: string): Decimal {
  return new Decimal(portfolioMinorUnits).mul(drawRate).toDecimalPlaces(0, Decimal.ROUND_HALF_UP);
}
