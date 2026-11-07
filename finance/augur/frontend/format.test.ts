import { test, expect } from "vitest";

import { fanChartAxis, fmtAxisMetricValue } from "./lib/chart";
import { currencyQuantaChartNumber, fmtQuanta, fmtNumber, fmtQuantity } from "./lib/format";

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

test("fmtQuanta preserves an Int64 quantum count without Number coercion", () => {
  expect(fmtQuanta("9007199254740993", { currencyCode: "USD", currencyQuantum: "0.01" })).toBe(
    "USD\u00a090,071,992,547,409.93"
  );
});

test("fmtQuanta renders zero-decimal and non-cent currency quanta", () => {
  expect(fmtQuanta("123456", { currencyCode: "JPY", currencyQuantum: "1" })).toBe("JPY\u00a0123,456");
  expect(fmtQuanta("123456", { currencyCode: "MGA", currencyQuantum: "0.05" })).toBe("MGA\u00a06,172.80");
});

test("chart conversion is isolated from exact label rendering", () => {
  expect(currencyQuantaChartNumber("123456", "0.05")).toBe(6172.8);
});

test("snake-case wire metrics retain rounded currency chart axes and labels", () => {
  const axis = fanChartAxis("net_worth_quanta", [5100, 11900]);

  expect(axis.ticks).toEqual([12000, 10000, 8000, 6000, 4000]);
  expect(fmtAxisMetricValue("net_worth_quanta", axis.ticks[0], { currencyCode: "USD" })).toBe("USD\u00a012,000");
});

test("snake-case percentage metrics retain percent labels", () => {
  expect(fmtAxisMetricValue("return_pct", 0.42)).toBe("42.0%");
});
