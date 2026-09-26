import { expect, test } from "vitest";

import { eventDetailText, eventLabel, sellableSecurities } from "./data_helpers";

test("TLH timeline renders signed effects and distinguishes zero-cash redemption from modeled loss", () => {
  const event = {
    kind: "tlh_financial_effect",
    operation: "redemption",
    portfolioId: "test-managed",
    amountQuanta: "0",
    shortTermGainQuanta: "0",
    longTermGainQuanta: "-1",
    basisChangeQuanta: "-1",
    interestIncomeQuanta: "0",
    _currency: { currencyCode: "USD", currencyQuantum: "1" },
  };
  expect(eventLabel(event)).toBe("TLH redemption: test-managed");
  expect(eventDetailText(event)).toBe(
    "cash USD\u00a00; ST gain USD\u00a00; LT gain USD\u00a0-1; basis change USD\u00a0-1; interest USD\u00a00"
  );
  expect(eventLabel({ ...event, operation: "modeled_realization" })).toBe("TLH modeled realization: test-managed");
});

test("a TLH portfolio is a sellable sleeve keyed by its index, merged with holdings of that index", () => {
  const portfolio = {
    holdings: [
      {
        label: "Test ETF",
        symbol: "TETF",
        asset: { kind: "security", symbol: "test-index" },
        currentValueQuanta: "100",
      },
      { label: "Test private", asset: { kind: "private_equity", issuerId: "test-issuer" }, currentValueQuanta: "7" },
    ],
    tlhPortfolios: [
      { label: "Test managed", asset: { kind: "security", symbol: "test-index" }, currentValueQuanta: "250" },
      { label: "Test managed bonds", asset: { kind: "security", symbol: "test-bonds" }, currentValueQuanta: "40" },
    ],
  };
  expect(sellableSecurities(portfolio)).toEqual([
    { symbol: "test-index", label: "Test ETF + Test managed", valueQuanta: "350" },
    { symbol: "test-bonds", label: "Test managed bonds", valueQuanta: "40" },
  ]);
});
