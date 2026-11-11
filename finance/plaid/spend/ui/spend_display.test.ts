import { describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import Decimal from "decimal.js";

import { money, time } from "./spend_display";

type CurrencyVector = {
  currency: string;
  currency_exponent: number;
  major_amount: string;
  minor_units: string;
  expected_magnitude: string;
  below_threshold: boolean;
};

const currencyVectors = JSON.parse(
  readFileSync(
    join(
      process.env.TEST_SRCDIR ?? "",
      process.env.TEST_WORKSPACE ?? "",
      "personal_dashboard/shared/currency_policy_vectors.json"
    ),
    "utf8"
  )
) as { cases: CurrencyVector[] };

function expectCurrencyVector(actual: string, vector: CurrencyVector): void {
  const match = actual.match(/(?<!\d)(\d[\d,]*(?:\.\d+)?)/);
  expect(match, actual).not.toBeNull();
  if (!match) return;
  expect(Number(match[1].replaceAll(",", ""))).toBe(Number(vector.expected_magnitude));
  const prefix = actual.slice(0, match.index);
  expect(prefix.includes("-")).toBe(Number(vector.major_amount) < 0);
  expect(prefix.includes("<")).toBe(vector.below_threshold);
}

describe("summary money", () => {
  it.each([
    [0, "USD", "$0"],
    [1, "USD", "$0.01"],
    [-1, "USD", "-$0.01"],
    [300, "USD", "$3"],
    [999, "USD", "$9.99"],
    [1000, "USD", "$10"],
    [1250, "USD", "$13"],
    [9, "JPY", "¥9"],
    [1, "KWD", "KWD 0.001"],
  ])("formats %s minor units of %s", (amount, currency, expected) => {
    expect(money(amount, currency)).toBe(expected);
  });

  it.each(currencyVectors.cases)("matches shared numeric policy vector $currency $major_amount", (vector) =>
    expectCurrencyVector(money(new Decimal(vector.minor_units), vector.currency), vector)
  );

  it("keeps exact amounts available in details", () => {
    expect(money(1250, "USD", true)).toBe("$12.50");
  });

  it("does not round a sub-cent nonzero computed amount to zero", () => {
    expect(money(0.1, "USD")).toBe("<$0.01");
    expect(money(-0.1, "USD")).toBe("-<$0.01");
    expect(money(0.1, "USD", true)).toBe("<$0.01");
  });

  it("uses the same currency presentation as the CLI when the host locale differs", () => {
    const nativeNumberFormat = Intl.NumberFormat;
    const germanDefault = new Proxy(nativeNumberFormat, {
      construct(target, args, newTarget) {
        return Reflect.construct(target, [args[0] ?? "de-DE", args[1]], newTarget);
      },
    });
    vi.stubGlobal("Intl", Object.create(Intl, { NumberFormat: { value: germanDefault } }));
    try {
      expect(new Intl.NumberFormat(undefined, { style: "currency", currency: "USD" }).format(1234.56)).toBe(
        "1.234,56 $"
      );
      expect(money(123456, "USD")).toBe("$1,235");
      expect(money(123456, "USD", true)).toBe("$1,234.56");
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("summary timestamps", () => {
  it("uses local time and shows a year or clock only when needed", () => {
    const now = new Date(2026, 11, 20, 12, 0);
    const nearby = new Date(now.getTime() + 14 * 86400_000);
    const distant = new Date(nearby.getTime() + 1000);
    expect(time(nearby.toISOString(), now)).toBe("Jan 3, 2027, 12:00");
    expect(time(distant.toISOString(), now)).toBe("Jan 3, 2027");
    expect(time(new Date(2026, 11, 19, 12).toISOString(), now)).toBe("Dec 19, 12:00");
    expect(time("not a date", now)).toBe("Unknown");
  });
});
