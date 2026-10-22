import type { components } from "./api/schema";

export type Allowance = components["schemas"]["AllowanceView"];
export type CardView = components["schemas"]["CardView"];
export type SpendView = components["schemas"]["SpendView"];
export type SpendConfiguration = components["schemas"]["SpendConfigurationView"];
export type TransactionsView = components["schemas"]["SpendTransactionsView"];
export type TransactionRow = components["schemas"]["SpendTransactionRow"];
export type TransactionPeriodId = TransactionsView["requested_period_id"];
export type AllowancePeriodId = components["schemas"]["Period"]["id"];
export type EstimatePeriodId = Extract<AllowancePeriodId, "rolling_7d" | "rolling_30d">;
export type RuleCondition = components["schemas"]["Rule"]["condition"];
export type RuleKind = components["schemas"]["Rule"]["kind"];
export type SpendTab = "spending" | "transactions" | "configuration";
