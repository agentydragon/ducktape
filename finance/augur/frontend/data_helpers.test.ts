import { expect, test } from "vitest";

import { eventDetailText, eventLabel, sellableSleeves } from "./data_helpers";
import { resolveSleeveWeights } from "./input_helpers";
import { fmtQuanta } from "./lib/format";

type Portfolio = NonNullable<Parameters<typeof sellableSleeves>[0]>;

const emptyPortfolio: Portfolio = {
  as_of_date: "2026-10-27",
  currency_code: "USD",
  currency_quantum: "1",
  cash_quanta: "0",
  holdings: [],
  tlh_portfolios: [],
  bonds: [],
  total_holdings_value_quanta: "0",
  total_holdings_cost_basis_quanta: "0",
  total_bond_face_value_quanta: "0",
};

const holdingDefaults = {
  position_id: "test-position",
  account_id: "test-account",
  symbol: "",
  unit_value_quanta: "1",
  quantity: 1,
  total_cost_basis_quanta: "0",
  lots: [],
} satisfies Omit<Portfolio["holdings"][number], "asset" | "current_value_quanta">;

const managedDefaults = {
  owner_agent_id: "test-owner",
  account_id: "test-account",
  total_cost_basis_quanta: "0",
  cohorts: [],
} satisfies Omit<Portfolio["tlh_portfolios"][number], "portfolio_id" | "label" | "asset" | "current_value_quanta">;

test("TLH timeline renders signed effects and distinguishes zero-cash redemption from modeled loss", () => {
  const currency = { currencyCode: "USD", currencyQuantum: "1" };
  const event = {
    kind: "tlh_financial_effect",
    month_index: 0,
    cause_id: "test-effect",
    account_id: "test-account",
    operation: "redemption",
    portfolio_id: "test-managed",
    amount_quanta: "0",
    short_term_gain_quanta: "2",
    long_term_gain_quanta: "-3",
    basis_change_quanta: "-4",
    income_quanta: "5",
    _currency: currency,
  } satisfies NonNullable<Parameters<typeof eventLabel>[0]>;
  const redemption = eventLabel(event);
  const modeledRealization = eventLabel({ ...event, operation: "modeled_realization" });
  expect(redemption).toContain("test-managed");
  expect(modeledRealization).toContain("test-managed");
  expect(redemption).not.toBe(modeledRealization);
  const detail = eventDetailText(event);
  for (const quanta of ["0", "2", "-3", "-4", "5"]) {
    expect(detail).toContain(fmtQuanta(quanta, currency));
  }
});

test("each TLH portfolio is its own sleeve, labelled as itself and apart from lots of its index", () => {
  const portfolio = {
    ...emptyPortfolio,
    total_holdings_value_quanta: "127",
    holdings: [
      {
        ...holdingDefaults,
        label: "Test ETF",
        symbol: "TETF",
        asset: { kind: "security", symbol: "test-index" },
        current_value_quanta: "100",
      },
      {
        ...holdingDefaults,
        position_id: "test-twin",
        label: "Test ETF twin",
        symbol: "TTWN",
        asset: { kind: "security", symbol: "test-index" },
        current_value_quanta: "20",
      },
      {
        ...holdingDefaults,
        position_id: "test-private",
        label: "Test private",
        asset: { kind: "private_equity", issuer_id: "test-issuer" },
        current_value_quanta: "7",
      },
    ],
    tlh_portfolios: [
      {
        ...managedDefaults,
        portfolio_id: "test-managed",
        label: "Test managed",
        asset: { kind: "security", symbol: "test-index" },
        current_value_quanta: "250",
      },
      {
        ...managedDefaults,
        portfolio_id: "test-managed-bonds",
        label: "Test managed bonds",
        asset: { kind: "security", symbol: "test-bonds" },
        current_value_quanta: "40",
      },
    ],
  } satisfies Portfolio;
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
    ...emptyPortfolio,
    total_holdings_value_quanta: "300",
    holdings: [
      {
        ...holdingDefaults,
        label: "Test ETF",
        asset: { kind: "security", symbol: "test-index" },
        current_value_quanta: "300",
      },
    ],
    tlh_portfolios: [
      {
        ...managedDefaults,
        portfolio_id: "test-managed",
        label: "Test managed",
        asset: { kind: "security", symbol: "test-index" },
        current_value_quanta: "100",
      },
    ],
  });
  expect(resolveSleeveWeights(null, sellable)).toEqual([
    { kind: "security", symbol: "test-index", weight: 75 },
    { kind: "managed_portfolio", portfolioId: "test-managed", weight: 25 },
  ]);
});
