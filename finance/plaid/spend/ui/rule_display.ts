import type { RuleCondition, RuleKind } from "./types";

export const ruleKindDisplay = {
  fixed: { label: "Mandatory", color: "blue" },
  excluded: { label: "Excluded", color: "gray" },
  review: { label: "Review", color: "orange" },
  flexible: { label: "Flexible", color: "teal" },
} satisfies Record<RuleKind, { label: string; color: string }>;

export const rulesFootnote =
  "Rules are checked in order and the first match governs the transaction, so a one-off correction sits above the patterns it narrows. A one-off names one observed transaction by a bounded date-and-amount match rather than a Plaid transaction id, which relinking rewrites. Unmatched purchases count as flexible.";

export function ruleConditionText(condition: RuleCondition): string {
  switch (condition.type) {
    case "name_prefix":
      return `${condition.field === "name" ? "Transaction name" : "Merchant name"} starts with “${condition.prefix}”`;
    case "name_contains":
      return `${condition.field === "name" ? "Transaction name" : "Merchant name"} contains “${condition.substring}”`;
    case "category_exact":
      return `${condition.field} equals ${condition.value}`;
    case "amount_exact":
      return `Amount equals ${condition.value} USD`;
    case "amount_sign":
      return `Amount is ${condition.sign}`;
    case "date_range":
      if (condition.start && condition.end)
        return condition.start === condition.end
          ? `Date is ${condition.start}`
          : `Date is from ${condition.start} through ${condition.end}`;
      if (condition.start) return `Date is on or after ${condition.start}`;
      return `Date is on or before ${condition.end}`;
    case "field_exact":
      return `${condition.field === "merchant_category_code" ? "Merchant category code" : condition.field} equals ${String(condition.value)}`;
    case "counterparty_exact":
      return `${condition.counterparty_type} counterparty is “${condition.name}”`;
    case "any_of":
      return condition.conditions.map(ruleConditionText).join(" OR ");
    case "all_of":
      return condition.conditions
        .map((part) => (part.type === "any_of" ? `(${ruleConditionText(part)})` : ruleConditionText(part)))
        .join(" AND ");
  }
}
