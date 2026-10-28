import { useState, type ReactNode } from "react";
import {
  Accordion,
  Alert,
  Badge,
  Box,
  Button,
  Divider,
  Group,
  Paper,
  ScrollArea,
  SegmentedControl,
  SimpleGrid,
  Stack,
  Table,
  Text,
  Title,
} from "@mantine/core";
import IconChevronDown from "@tabler/icons-react/dist/esm/icons/IconChevronDown.mjs";
import IconChevronRight from "@tabler/icons-react/dist/esm/icons/IconChevronRight.mjs";

import type { AllowanceView, SpendTransactionRow, SpendTransactionsView } from "./api/schema";
import { HelpTooltip } from "../../../../util/frontend/help_tooltip";
import { Metric, Money, money, periodLabels, time } from "./spend_display";
import { ruleConditionText, ruleKindDisplay } from "./rule_display";
import type { AllowancePeriodId, RuleKind, TransactionPeriodId } from "./types";

function spendPeriod(allowance: AllowanceView, periodId: AllowancePeriodId) {
  return allowance.spend_periods.find((report) => report.period.id === periodId);
}

const dispositionText = {
  counted: "Counted in allowance",
  pace_only: "Pace only · before allowance start",
  fixed: "Mandatory · outside allowance",
  excluded: "Excluded from allowance",
  reversed: "Full reversal · excluded from allowance",
  held_refund: "Refund held for review",
  superseded_pending: "Pending version replaced by posted charge",
  other_currency: "Different currency · outside allowance",
} satisfies Record<NonNullable<SpendTransactionRow["disposition"]>, string>;

const statementText = {
  counted: "Counted in card cycle",
  outside_cycle: "Outside current card cycle",
  superseded_pending: "Pending version replaced by posted charge",
  card_payment: "Card payment excluded from card spend",
  other_currency: "Different currency excluded from card spend",
  unavailable: "Card cycle unavailable",
} satisfies Record<NonNullable<SpendTransactionRow["statement_reason"]>, string>;

function isReviewRow(row: SpendTransactionRow): boolean {
  // The server resolves which rule governs the money (first match in the ordered list); the client only reads it.
  const kind = row.effective_kind;
  return (
    row.allowance_in_scope &&
    ((row.disposition === "counted" && (kind == null || kind === "review")) || row.disposition === "held_refund")
  );
}

const dispositionKindText = {
  fixed: "Counted as mandatory",
  excluded: "Excluded",
  review: "Counted as flexible pending review",
  flexible: "Counted as flexible",
} satisfies Record<RuleKind, string>;

function classificationForRow(row: SpendTransactionRow): { label: string; color: string } {
  if (row.disposition === "reversed") return { label: "Full reversal", color: "teal" };
  if (row.disposition === "held_refund") return { label: "Refund held", color: "orange" };
  if (row.disposition === "superseded_pending") return { label: "Superseded", color: "gray" };
  if (row.disposition === "other_currency") return { label: "Other currency", color: "gray" };
  if (row.rule)
    return row.rule.scope === "one_off"
      ? { label: `${ruleKindDisplay[row.rule.kind].label} (one-off)`, color: "violet" }
      : ruleKindDisplay[row.rule.kind];
  if (!row.allowance_in_scope) return { label: "Outside allowance", color: "gray" };
  if (row.disposition === null) return { label: "Unavailable", color: "gray" };
  return { label: "Unmatched", color: "orange" };
}

function categoryForRow(row: SpendTransactionRow): string {
  return row.category?.label || row.rule?.analysis_category || "No category inferred";
}

function CategoryMarker({ row }: { row: SpendTransactionRow }): ReactNode {
  if (!row.category) return null;
  return (
    <Box
      component="span"
      aria-hidden="true"
      data-category-id={row.category.id}
      style={{
        display: "inline-block",
        width: 10,
        height: 10,
        flex: "0 0 auto",
        borderRadius: "50%",
        backgroundColor: row.category.color,
      }}
    />
  );
}

function CompactCounterparties({ counterparties }: { counterparties: SpendTransactionRow["counterparties"] }) {
  if (!counterparties?.length) return null;
  return (
    <Text size="xs" c="dimmed" lineClamp={1}>
      Counterparties: {counterparties.map((counterparty) => counterparty.name || "Unnamed").join(", ")}
    </Text>
  );
}

function TransactionTitle({ row }: { row: SpendTransactionRow }) {
  const title = row.merchant_name || row.name;
  const counterparties = row.counterparties || [];
  const merchants = counterparties.filter((party) => party.type?.toLowerCase() === "merchant");
  // A single merchant with at most one other counterparty fits in the title line.
  // Keep unusual/multi-party data in the original separate line rather than hiding it.
  if (merchants.length === 1 && counterparties.length <= 2) {
    const others = counterparties.filter((party) => party.name?.toLowerCase() !== title.toLowerCase());
    const suffix = others.map((party) => party.name || "Unnamed").join(" · ");
    return (
      <Text size="sm" fw={650} lineClamp={1} title={suffix ? `${title} · ${suffix}` : title}>
        {title}
        {suffix && (
          <Text span fw={400} c="dimmed">
            {" "}
            · {suffix}
          </Text>
        )}
      </Text>
    );
  }
  return (
    <>
      <Text size="sm" fw={650} lineClamp={1} title={title}>
        {title}
      </Text>
      <CompactCounterparties counterparties={row.counterparties} />
    </>
  );
}

type PlaidFieldValue = string | number | null | undefined;

function hasSuppliedValue(value: unknown): boolean {
  if (value == null || value === "") return false;
  if (Array.isArray(value)) return value.some(hasSuppliedValue);
  if (typeof value === "object") return Object.values(value).some(hasSuppliedValue);
  return true;
}

function PlaidFieldGroup({ title, fields }: { title: string; fields: Array<[string, PlaidFieldValue]> }) {
  const supplied = fields.filter(([, value]) => hasSuppliedValue(value));
  if (supplied.length === 0) return null;
  return (
    <Stack gap="xs">
      <Text size="sm" fw={650}>
        {title}
      </Text>
      <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="xs">
        {supplied.map(([label, value]) => (
          <Text key={label} size="sm" style={{ overflowWrap: "anywhere" }}>
            <strong>{label}:</strong> {value}
          </Text>
        ))}
      </SimpleGrid>
    </Stack>
  );
}

function PlaidSourceFields({ row }: { row: SpendTransactionRow }) {
  const details = row.details;
  return (
    <Accordion variant="contained">
      <Accordion.Item value="plaid-fields">
        <Accordion.Control>Plaid source fields</Accordion.Control>
        <Accordion.Panel>
          <Text size="xs" c="dimmed" mb="sm">
            Original amount and running balance are in major currency units. The table and allowance effects use integer
            minor units.
          </Text>
          {hasSuppliedValue(details) ? (
            <Stack gap="md">
              <PlaidFieldGroup
                title="Source and merchant"
                fields={[
                  ["Plaid amount (major units)", details?.amount],
                  ["ISO currency code", details?.iso_currency_code],
                  ["Unofficial currency code", details?.unofficial_currency_code],
                  ["Original description", details?.original_description],
                  ["Plaid account ID", details?.account_id],
                  ["Plaid transaction ID", details?.transaction_id],
                  ["Pending transaction ID", details?.pending_transaction_id],
                  ["Account owner", details?.account_owner],
                  ["Check number", details?.check_number],
                  ["Payment channel", details?.payment_channel],
                  ["Transaction type", details?.transaction_type],
                  ["Transaction code", details?.transaction_code],
                  ["Merchant entity ID", details?.merchant_entity_id],
                  ["Website", details?.website],
                  ["Logo URL", details?.logo_url],
                  ["Category icon URL", details?.personal_finance_category_icon_url],
                  ["Running balance (major units)", details?.running_balance],
                  ["Custom entity ID", details?.client_customization?.custom_entity_id],
                ]}
              />
              <PlaidFieldGroup
                title="Timing and categories"
                fields={[
                  ["Plaid date", row.plaid_date ?? row.date],
                  ["Authorized date", details?.authorized_date],
                  ["Authorized time", details?.authorized_datetime],
                  ["Posted time", details?.datetime],
                  ["Personal category", details?.personal_finance_category?.primary],
                  ["Personal detail", details?.personal_finance_category?.detailed],
                  ["Personal category confidence", details?.personal_finance_category?.confidence_level],
                  ["Personal category version", details?.personal_finance_category?.version],
                  ["Business category", details?.business_finance_category?.primary],
                  ["Business detail", details?.business_finance_category?.detailed],
                  ["Business category confidence", details?.business_finance_category?.confidence_level],
                  ["Legacy categories", details?.category?.join(" / ")],
                  ["Legacy category ID", details?.category_id],
                ]}
              />
              <PlaidFieldGroup
                title="Location"
                fields={[
                  ["Address", details?.location?.address],
                  ["City", details?.location?.city],
                  ["Region", details?.location?.region],
                  ["Postal code", details?.location?.postal_code],
                  ["Country", details?.location?.country],
                  ["Latitude", details?.location?.lat],
                  ["Longitude", details?.location?.lon],
                  ["Store number", details?.location?.store_number],
                ]}
              />
              <PlaidFieldGroup
                title="Transfer metadata"
                fields={[
                  ["Reference number", details?.payment_meta?.reference_number],
                  ["PPD ID", details?.payment_meta?.ppd_id],
                  ["Payee", details?.payment_meta?.payee],
                  ["By order of", details?.payment_meta?.by_order_of],
                  ["Payer", details?.payment_meta?.payer],
                  ["Payment method", details?.payment_meta?.payment_method],
                  ["Payment processor", details?.payment_meta?.payment_processor],
                  ["Reason", details?.payment_meta?.reason],
                ]}
              />
            </Stack>
          ) : (
            <Text size="sm" c="dimmed">
              No additional Plaid fields supplied.
            </Text>
          )}
        </Accordion.Panel>
      </Accordion.Item>
    </Accordion>
  );
}

function TransactionDetails({ row, currency }: { row: SpendTransactionRow; currency: string }) {
  const m = (value: number | null | undefined) => <Money value={value} currency={currency} />;
  return (
    <Stack gap="sm">
      <Text size="sm">
        <strong>Allowance:</strong>{" "}
        {row.disposition
          ? dispositionText[row.disposition]
          : row.allowance_in_scope
            ? "Unavailable"
            : "Account outside allowance"}
        .
        {isReviewRow(row) &&
          row.disposition === "counted" &&
          " This charge is counted as flexible while its classification is reviewed."}
      </Text>
      {row.reversal_pair && (
        <Stack gap="xs" aria-label={`Full reversal pair ${row.reversal_pair.id}`}>
          <Group gap="xs" wrap="wrap">
            <Badge color="teal" variant="light">
              Full reversal pair
            </Badge>
            <Badge color={row.amount_minor_units > 0 ? "blue" : "teal"} variant="light">
              {row.amount_minor_units > 0 ? "Original debit" : "Reversal credit"}
            </Badge>
            <Text size="sm" fw={650} style={{ overflowWrap: "anywhere" }}>
              {row.reversal_pair.id}
            </Text>
          </Group>
          <Text size="sm" c="dimmed">
            {row.reversal_pair.description}
          </Text>
          <Text size="sm" style={{ overflowWrap: "anywhere" }}>
            <strong>Original debit selector:</strong> {ruleConditionText(row.reversal_pair.original)}
          </Text>
          <Text size="sm" style={{ overflowWrap: "anywhere" }}>
            <strong>Reversal credit selector:</strong> {ruleConditionText(row.reversal_pair.reversal)}
          </Text>
        </Stack>
      )}
      {row.rule && (
        <Stack gap={2}>
          <Group gap="xs" wrap="wrap">
            <Text size="sm" component="span">
              <strong>Rule #{row.rule_number}:</strong> {ruleConditionText(row.rule.condition)}
            </Text>
            {row.rule.scope === "one_off" && (
              <Badge color="violet" variant="light" size="xs">
                One-off
              </Badge>
            )}
          </Group>
          <Text size="sm">
            {row.reversal_pair
              ? "Classification rule is shown as context; the full reversal pair controls the zero allowance effect."
              : dispositionKindText[row.rule.kind]}
          </Text>
          {row.rule.id && (
            <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
              {row.rule.id}
            </Text>
          )}
          {row.rule.description && (
            <Text size="sm" c="dimmed">
              {row.rule.description}
            </Text>
          )}
          {row.category && (
            <Text size="xs" c="dimmed">
              Analysis category: {categoryForRow(row)}
              {row.category.id !== "unclassified" && ` (${row.category.id})`}
            </Text>
          )}
          <Button component="a" href="#/configuration" variant="subtle" size="xs" w="fit-content" px={0}>
            View all rules
          </Button>
        </Stack>
      )}
      <Divider />
      <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="xs">
        <Text size="sm">
          Allowance balance effect: {m(row.allowance_minor_units === 0 ? 0 : -row.allowance_minor_units)}
        </Text>
        {row.pace_effects.map((effect) => (
          <Text size="sm" key={effect.period_id}>
            {periodLabels[effect.period_id]} pace input: {m(effect.amount_minor_units)}
          </Text>
        ))}
        <Text size="sm">
          Card statement:{" "}
          {row.statement_reason
            ? `${statementText[row.statement_reason]} · ${money(row.statement_minor_units, row.currency, true)}`
            : "Not a configured card"}
        </Text>
      </SimpleGrid>
      <Text size="xs" c="dimmed">
        Plaid name: {row.name} · merchant: {row.merchant_name || "Unknown"} · category:{" "}
        {row.pfc_detailed || row.pfc_primary || "Unknown"} · merchant category code:{" "}
        {row.merchant_category_code || "Unknown"}
      </Text>
      {(row.counterparties?.length ?? 0) > 0 && (
        <Stack gap={2}>
          <Text size="sm" fw={650}>
            Plaid counterparties
          </Text>
          {row.counterparties?.map((counterparty, index) => (
            <PlaidFieldGroup
              key={index}
              title={`${counterparty.name || "Unnamed"} · ${counterparty.type || "Unknown type"}`}
              fields={[
                ["Confidence", counterparty.confidence_level],
                ["Entity ID", counterparty.entity_id],
                ["Website", counterparty.website],
                ["Logo URL", counterparty.logo_url],
                ["Bacs account", counterparty.account_numbers?.bacs?.account],
                ["Bacs sort code", counterparty.account_numbers?.bacs?.sort_code],
                ["IBAN", counterparty.account_numbers?.international?.iban],
                ["BIC", counterparty.account_numbers?.international?.bic],
              ]}
            />
          ))}
        </Stack>
      )}
      <PlaidSourceFields row={row} />
    </Stack>
  );
}

export function TransactionsPage({
  transactions,
  loading,
  error,
  periodId,
  onPeriodChange,
}: {
  transactions: SpendTransactionsView | null;
  loading: boolean;
  error: string | null;
  periodId: TransactionPeriodId;
  onPeriodChange: (periodId: TransactionPeriodId) => void;
}) {
  const [filter, setFilter] = useState<"all" | "review" | "effect">("all");
  const [expandedRow, setExpandedRow] = useState<number | null>(null);
  const rows = transactions?.rows ?? [];
  const shown = rows
    .map((row, index) => ({ row, index }))
    .filter(({ row }) =>
      filter === "review" ? isReviewRow(row) : filter === "effect" ? row.allowance_minor_units !== 0 : true
    );
  const allowance = transactions?.allowance;
  const currency = allowance?.currency ?? "USD";
  const m = (value: number | null | undefined) => <Money value={value} currency={currency} />;

  return (
    <Stack gap="lg">
      <Group justify="space-between" align="flex-start" gap="md">
        <div>
          <Group gap="xs" align="center">
            <Title order={1} size="h3">
              Transactions
            </Title>
            <HelpTooltip
              topic="Transactions"
              description="Positive amounts are charges; negative amounts are credits. Allowance shows the spending counted against your flexible budget; an em dash means no effect. Badges show classification decisions. Open a row for its matching rule and explanation."
            />
          </Group>
        </div>
        <Badge color="gray" variant="light">
          Read only
        </Badge>
      </Group>
      <Group justify="space-between" align="end" gap="md">
        <SegmentedControl
          aria-label="Transaction period"
          value={periodId}
          onChange={(value) => onPeriodChange(value as TransactionPeriodId)}
          data={[
            { label: "7 days", value: "rolling_7d" },
            { label: "30 days", value: "rolling_30d" },
            { label: "Credit cycle", value: "credit_cycle" },
          ]}
        />
        <SegmentedControl
          aria-label="Transaction filter"
          value={filter}
          onChange={(value) => setFilter(value as typeof filter)}
          data={[
            { label: "All", value: "all" },
            { label: "Review", value: "review" },
            { label: "Allowance effect", value: "effect" },
          ]}
        />
      </Group>
      {error && (
        <Alert color="red" title="Couldn't load transactions">
          {error}
        </Alert>
      )}
      {loading && (
        <Text size="sm" c="dimmed">
          Refreshing transactions…
        </Text>
      )}
      {transactions && (
        <>
          {allowance?.status !== "active" && (
            <Alert color="yellow" title="Allowance classification unavailable">
              {allowance?.note ?? "No flexible allowance is configured."} Card transactions may still appear below.
            </Alert>
          )}
          {transactions.period.id !== periodId && (
            <Text size="sm" c="dimmed">
              Showing the last 30 days because the credit cycle is unavailable.
            </Text>
          )}
          <Paper withBorder radius="lg" p="lg">
            <Stack gap="md">
              <SimpleGrid cols={{ base: 1, sm: 3 }} spacing="md">
                <Metric
                  label="TRANSACTIONS IN PERIOD"
                  value={transactions.summary.transaction_count}
                  detail={`Since ${transactions.period.start}`}
                />
                <Metric
                  label="NET ALLOWANCE SPEND"
                  value={
                    allowance?.status === "active"
                      ? m(transactions.summary.net_allowance_spend_minor_units)
                      : "Unavailable"
                  }
                  detail="Positive charges less accepted refunds"
                />
                <Metric
                  label="NEEDS CLASSIFICATION"
                  value={
                    allowance?.status === "active"
                      ? `${transactions.summary.unmatched_charge_count} · ${money(
                          transactions.summary.unmatched_charge_minor_units,
                          currency
                        )}`
                      : "Unavailable"
                  }
                  detail="Unmatched charges count; held refunds do not"
                />
              </SimpleGrid>
              {periodId === "credit_cycle" && allowance?.status === "active" && (
                <>
                  <Divider />
                  <Text size="sm" fw={650}>
                    Allowance bridge
                  </Text>
                  <Text size="sm">
                    {m(allowance.prior_carry_minor_units)} carried + {m(allowance.monthly_minor_units)} monthly credit −{" "}
                    {m(spendPeriod(allowance, "credit_cycle")?.spend_minor_units)} cycle spend ={" "}
                    <strong>{m(allowance.available_minor_units)} available</strong>
                  </Text>
                  <Text size="xs" c="dimmed">
                    Cycle spend includes pending charges. A held refund does not restore the allowance until matched by
                    an explicit rule. The card statement uses a separate cycle and may include mandatory purchases.
                  </Text>
                </>
              )}
            </Stack>
          </Paper>
          <Group justify="space-between" align="baseline" gap="sm">
            <Text size="sm" c="dimmed">
              Showing {shown.length} of {rows.length}
            </Text>
            <Text size="xs" c="dimmed">
              View updated {time(transactions.generated_at)} · oldest allowance sync {time(allowance?.last_synced_at)}
            </Text>
          </Group>
          {shown.length === 0 && (
            <Text size="sm" c="dimmed">
              No transactions match this period and filter.
            </Text>
          )}
          <Box visibleFrom="md">
            <ScrollArea type="auto">
              <Table miw={850} verticalSpacing="sm" horizontalSpacing="md" withTableBorder>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Date</Table.Th>
                    <Table.Th>Merchant</Table.Th>
                    <Table.Th>Account</Table.Th>
                    <Table.Th>Category</Table.Th>
                    <Table.Th ta="right">
                      <Group gap={4} justify="flex-end" wrap="nowrap">
                        Amount
                        <HelpTooltip
                          topic="Transaction amount"
                          description="Positive amounts are charges; negative amounts are credits."
                        />
                      </Group>
                    </Table.Th>
                    <Table.Th ta="right">
                      <Group gap={4} justify="flex-end" wrap="nowrap">
                        Allowance
                        <HelpTooltip
                          topic="Allowance effect"
                          description="Shows the transaction's effect on flexible allowance spending. An em dash means there is no effect. Open a row for its disposition and matching rule."
                        />
                      </Group>
                    </Table.Th>
                  </Table.Tr>
                </Table.Thead>
                {shown.map(({ row, index }) => {
                  const classification = classificationForRow(row);
                  const expanded = expandedRow === index;
                  return (
                    <Table.Tbody className="transaction-rows" key={`${row.date}-${row.account_label}-${index}`}>
                      <Table.Tr
                        data-transaction-row
                        tabIndex={0}
                        aria-label={`${expanded ? "Hide" : "Show"} details for ${row.merchant_name || row.name}`}
                        aria-expanded={expanded}
                        onClick={() => setExpandedRow(expanded ? null : index)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter" || event.key === " ") {
                            event.preventDefault();
                            setExpandedRow(expanded ? null : index);
                          }
                        }}
                        style={{ cursor: "pointer" }}
                      >
                        <Table.Td style={{ whiteSpace: "nowrap" }}>{row.date}</Table.Td>
                        <Table.Td>
                          <Group gap="xs" wrap="nowrap">
                            <Box c="dimmed" aria-hidden="true">
                              {expanded ? <IconChevronDown size={16} /> : <IconChevronRight size={16} />}
                            </Box>
                            <Stack gap={0} miw={0} maw={320}>
                              <TransactionTitle row={row} />
                            </Stack>
                            {row.pending && (
                              <Badge size="xs" variant="light" color="yellow">
                                Pending
                              </Badge>
                            )}
                          </Group>
                        </Table.Td>
                        <Table.Td>{row.account_label}</Table.Td>
                        <Table.Td>
                          <Stack gap={2}>
                            <Group gap="xs" wrap="nowrap">
                              <CategoryMarker row={row} />
                              <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
                                {categoryForRow(row)}
                              </Text>
                            </Group>
                            <Badge size="sm" variant="light" color={classification.color} w="fit-content">
                              {classification.label}
                            </Badge>
                          </Stack>
                        </Table.Td>
                        <Table.Td ta="right" style={{ whiteSpace: "nowrap" }}>
                          {money(row.amount_minor_units, row.currency, true)}
                        </Table.Td>
                        <Table.Td ta="right" style={{ whiteSpace: "nowrap" }}>
                          {row.allowance_minor_units === 0 ? "—" : money(row.allowance_minor_units, currency, true)}
                        </Table.Td>
                      </Table.Tr>
                      {expanded && (
                        <Table.Tr>
                          <Table.Td colSpan={6}>
                            <TransactionDetails row={row} currency={currency} />
                          </Table.Td>
                        </Table.Tr>
                      )}
                    </Table.Tbody>
                  );
                })}
              </Table>
            </ScrollArea>
          </Box>
          <Box hiddenFrom="md">
            <Accordion variant="separated" radius="md">
              {shown.map(({ row, index }) => {
                const classification = classificationForRow(row);
                return (
                  <Accordion.Item key={`${row.date}-${row.account_label}-${index}`} value={String(index)}>
                    <Accordion.Control>
                      <Group justify="space-between" gap="sm" wrap="nowrap">
                        <Stack gap={2} miw={0} style={{ flex: "1 1 0" }}>
                          <TransactionTitle row={row} />
                          <Group gap="xs" wrap="nowrap">
                            <CategoryMarker row={row} />
                            <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                              {categoryForRow(row)}
                            </Text>
                          </Group>
                          <Group gap="xs">
                            <Text size="xs" c="dimmed">
                              {row.date} · {row.account_label}
                            </Text>
                            {row.pending && (
                              <Badge size="xs" variant="light" color="yellow">
                                Pending
                              </Badge>
                            )}
                            <Badge size="xs" variant="light" color={classification.color}>
                              {classification.label}
                            </Badge>
                          </Group>
                        </Stack>
                        <Stack gap={2} align="flex-end" style={{ flexShrink: 0 }}>
                          <Text fw={700} size="sm">
                            {money(row.amount_minor_units, row.currency, true)}
                          </Text>
                          <Text size="xs" c="dimmed">
                            Allowance{" "}
                            {row.allowance_minor_units === 0 ? "—" : money(row.allowance_minor_units, currency, true)}
                          </Text>
                        </Stack>
                      </Group>
                    </Accordion.Control>
                    <Accordion.Panel>
                      <TransactionDetails row={row} currency={currency} />
                    </Accordion.Panel>
                  </Accordion.Item>
                );
              })}
            </Accordion>
          </Box>
        </>
      )}
    </Stack>
  );
}
