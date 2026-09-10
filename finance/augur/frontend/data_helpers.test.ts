import { expect, test } from "vitest";

import { eventDetailText, eventLabel } from "./data_helpers";

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
