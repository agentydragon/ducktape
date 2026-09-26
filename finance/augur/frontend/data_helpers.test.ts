import { expect, test } from "vitest";

import { eventDetailText, eventLabel, sellableSleeves } from "./data_helpers";
import { resolveSleeveWeights } from "./input_helpers";

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

test("each TLH portfolio is its own sleeve, labelled as itself and apart from lots of its index", () => {
  const portfolio = {
    holdings: [
      {
        label: "Test ETF",
        symbol: "TETF",
        asset: { kind: "security", symbol: "test-index" },
        currentValueQuanta: "100",
      },
      {
        label: "Test ETF twin",
        symbol: "TTWN",
        asset: { kind: "security", symbol: "test-index" },
        currentValueQuanta: "20",
      },
      { label: "Test private", asset: { kind: "private_equity", issuerId: "test-issuer" }, currentValueQuanta: "7" },
    ],
    tlhPortfolios: [
      {
        portfolioId: "test-managed",
        label: "Test managed",
        asset: { kind: "security", symbol: "test-index" },
        currentValueQuanta: "250",
      },
      {
        portfolioId: "test-managed-bonds",
        label: "Test managed bonds",
        asset: { kind: "security", symbol: "test-bonds" },
        currentValueQuanta: "40",
      },
    ],
  };
  expect(sellableSleeves(portfolio)).toEqual([
    {
      key: "security:test-index",
      sleeve: { kind: "security", symbol: "test-index" },
      label: "Test ETF + Test ETF twin",
      valueQuanta: "120",
    },
    {
      key: "managed_portfolio:test-managed",
      sleeve: { kind: "managed_portfolio", portfolioId: "test-managed" },
      label: "Test managed",
      valueQuanta: "250",
    },
    {
      key: "managed_portfolio:test-managed-bonds",
      sleeve: { kind: "managed_portfolio", portfolioId: "test-managed-bonds" },
      label: "Test managed bonds",
      valueQuanta: "40",
    },
  ]);
});

test("an unedited target seeds a managed sleeve by portfolio id, beside the security on its index", () => {
  const sellable = sellableSleeves({
    holdings: [{ label: "Test ETF", asset: { kind: "security", symbol: "test-index" }, currentValueQuanta: "300" }],
    tlhPortfolios: [
      {
        portfolioId: "test-managed",
        label: "Test managed",
        asset: { kind: "security", symbol: "test-index" },
        currentValueQuanta: "100",
      },
    ],
  });
  expect(resolveSleeveWeights(null, sellable)).toEqual([
    { kind: "security", symbol: "test-index", weight: 75 },
    { kind: "managed_portfolio", portfolioId: "test-managed", weight: 25 },
  ]);
});
