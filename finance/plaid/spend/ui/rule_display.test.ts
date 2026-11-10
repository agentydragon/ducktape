import { expect, it } from "vitest";

import { ruleConditionText } from "./rule_display";

it("renders a bounded one-off with quoted fields and inclusive dates", () => {
  expect(
    ruleConditionText({
      type: "all_of",
      conditions: [
        { type: "date_range", start: "2026-01-03", end: "2026-01-03" },
        { type: "amount_exact", value: "42.50" },
        { type: "field_exact", field: "merchant_name", value: 'Example "Shop"' },
        { type: "field_exact", field: "account_type", value: "credit" },
      ],
    })
  ).toBe(
    'date == "2026-01-03" && amount == 42.50 USD && merchant_name == "Example \\"Shop\\"" && account_type == "credit"'
  );

  expect(ruleConditionText({ type: "date_range", start: "2026-01-01", end: "2026-01-31" })).toBe(
    'date >= "2026-01-01" && date <= "2026-01-31"'
  );
  expect(ruleConditionText({ type: "date_range", start: "2026-01-01" })).toBe('date >= "2026-01-01"');
  expect(ruleConditionText({ type: "date_range", end: "2026-01-31" })).toBe('date <= "2026-01-31"');
});

it("preserves OR grouping, sign and predicate details", () => {
  expect(
    ruleConditionText({
      type: "all_of",
      conditions: [
        { type: "amount_sign", sign: "negative" },
        {
          type: "any_of",
          conditions: [
            { type: "name_prefix", field: "name", prefix: "Example" },
            { type: "counterparty_exact", counterparty_type: "marketplace", name: "Market" },
          ],
        },
      ],
    })
  ).toBe('amount < 0 && (starts_with(name, "Example") || has_counterparty(type == "marketplace", name == "Market"))');
  expect(ruleConditionText({ type: "amount_sign", sign: "zero" })).toBe("amount == 0");
  expect(ruleConditionText({ type: "amount_sign", sign: "positive" })).toBe("amount > 0");
  expect(ruleConditionText({ type: "name_contains", field: "merchant_name", substring: "Cafe" })).toBe(
    'contains(merchant_name, "Cafe")'
  );
  expect(ruleConditionText({ type: "category_exact", field: "pfc_primary", value: "FOOD_AND_DRINK" })).toBe(
    'pfc_primary == "FOOD_AND_DRINK"'
  );
});
