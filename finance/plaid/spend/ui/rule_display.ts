import type { RuleCondition, RuleKind } from "./types";

export const ruleKindDisplay = {
  fixed: { label: "Mandatory", color: "blue" },
  excluded: { label: "Excluded", color: "gray" },
  review: { label: "Review", color: "orange" },
  flexible: { label: "Flexible", color: "teal" },
} satisfies Record<RuleKind, { label: string; color: string }>;

export const rulesFootnote =
  "Rules are checked in order and the first match governs the transaction, so a one-off correction sits above the patterns it narrows. A one-off names one observed transaction by a bounded date-and-amount match rather than a Plaid transaction id, which relinking rewrites. Name, field and counterparty string matches ignore case; Plaid category matches are case-sensitive. Unmatched purchases count as flexible.";

export function ruleConditionText(condition: RuleCondition): string {
  switch (condition.type) {
    case "name_prefix":
      return `starts_with(${condition.field}, ${JSON.stringify(condition.prefix)})`;
    case "name_contains":
      return `contains(${condition.field}, ${JSON.stringify(condition.substring)})`;
    case "category_exact":
      return `${condition.field} == ${JSON.stringify(condition.value)}`;
    case "amount_exact":
      return `amount == ${condition.value} USD`;
    case "amount_sign":
      return `amount ${condition.sign === "negative" ? "<" : condition.sign === "positive" ? ">" : "=="} 0`;
    case "date_range":
      if (condition.start && condition.end)
        return condition.start === condition.end
          ? `date == ${JSON.stringify(condition.start)}`
          : `date >= ${JSON.stringify(condition.start)} && date <= ${JSON.stringify(condition.end)}`;
      if (condition.start) return `date >= ${JSON.stringify(condition.start)}`;
      return `date <= ${JSON.stringify(condition.end)}`;
    case "field_exact":
      return `${condition.field} == ${JSON.stringify(condition.value)}`;
    case "counterparty_exact":
      return `has_counterparty(type == ${JSON.stringify(condition.counterparty_type)}, name == ${JSON.stringify(condition.name)})`;
    case "any_of":
      return condition.conditions.map(ruleConditionText).join(" || ");
    case "all_of":
      return condition.conditions
        .map((part) => (part.type === "any_of" ? `(${ruleConditionText(part)})` : ruleConditionText(part)))
        .join(" && ");
  }
}
