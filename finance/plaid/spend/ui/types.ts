import type { Period, Rule, SpendTransactionsView, SustainabilityView } from "./schema";

export type SustainabilityPortfolio = SustainabilityView["portfolio"];
export type SustainabilitySource = SustainabilityPortfolio["sources"][number];
export type SustainabilityHistory = SustainabilityView["history"][number];
export type TransactionPeriodId = SpendTransactionsView["requested_period_id"];
export type AllowancePeriodId = Period["id"];
export type EstimatePeriodId = Extract<AllowancePeriodId, "rolling_7d" | "rolling_30d">;
export type RuleCondition = Rule["condition"];
export type RuleKind = Rule["kind"];
export type SpendTab = "spending" | "transactions" | "configuration" | "sustainability";
