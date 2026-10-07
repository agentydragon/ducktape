import { Fragment, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { createRoot } from "react-dom/client";
import {
  Accordion,
  Alert,
  Anchor,
  Badge,
  Box,
  Button,
  Card,
  Center,
  Container,
  Divider,
  Grid,
  Group,
  MantineProvider,
  NumberInput,
  Paper,
  ScrollArea,
  SegmentedControl,
  SimpleGrid,
  Stack,
  Table,
  Tabs,
  Text,
  Title,
} from "@mantine/core";
import "@mantine/core/styles.css";
import type { components } from "./api/schema";

type Windows = components["schemas"]["Windows"];
type Allowance = components["schemas"]["AllowanceView"];
type CardView = components["schemas"]["CardView"];
type View = components["schemas"]["SpendView"];
type SpendConfiguration = components["schemas"]["SpendConfigurationView"];
type TransactionsView = components["schemas"]["SpendTransactionsView"];
type TransactionRow = components["schemas"]["SpendTransactionRow"];
type TransactionWindow = TransactionsView["window"];
type RuleCondition = components["schemas"]["Rule"]["condition"];
type RuleKind = components["schemas"]["Rule"]["kind"];
type SpendTab = "spending" | "transactions" | "configuration";

function tabForHash(hash: string): SpendTab {
  if (hash === "#/transactions") return "transactions";
  if (hash === "#/configuration") return "configuration";
  return "spending";
}

const ruleKindDisplay = {
  fixed: { label: "Mandatory", color: "blue" },
  excluded: { label: "Excluded", color: "gray" },
  review: { label: "Review", color: "orange" },
  flexible: { label: "Flexible", color: "teal" },
} satisfies Record<RuleKind, { label: string; color: string }>;

function money(value: number | null | undefined, currency: string | null, exact = false): string {
  if (value == null || !Number.isFinite(value)) return "Unavailable";
  const code = currency?.length === 3 ? currency.toUpperCase() : "USD";
  try {
    const formatter = new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: code,
      maximumFractionDigits: exact ? undefined : 0,
      minimumFractionDigits: exact ? undefined : 0,
    });
    const precision = new Intl.NumberFormat(undefined, { style: "currency", currency: code }).resolvedOptions()
      .maximumFractionDigits;
    const amount = value / 10 ** (precision ?? 2);
    if (!exact && amount !== 0 && Math.abs(amount) < 0.5) return `${amount < 0 ? "-" : ""}<${formatter.format(1)}`;
    return formatter.format(amount);
  } catch {
    const amount = value / 100;
    if (!exact && amount !== 0 && Math.abs(amount) < 0.5) return `${amount < 0 ? "-" : ""}<${code} 1`;
    return `${code} ${amount.toFixed(exact ? 2 : 0)}`;
  }
}

function Money({ value, currency }: { value: number | null | undefined; currency: string | null }): ReactNode {
  return <span title={money(value, currency, true)}>{money(value, currency)}</span>;
}
function time(value: string | null | undefined): string {
  if (!value || Number.isNaN(new Date(value).getTime())) return "Unknown";
  return new Date(value).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}
function cardTitle(card: CardView): string {
  return `${card.label || card.account_name || "Card"}${card.mask ? ` ···· ${card.mask}` : ""}`;
}
type Signal = "normal" | "warning" | "exceeded";

function signalFor(available: number, projected: number): Signal {
  return available <= 0 ? "exceeded" : projected < 0 ? "warning" : "normal";
}

function SignalLabel({ signal }: { signal: Signal }) {
  return (
    <Badge color={signal === "exceeded" ? "red" : signal === "warning" ? "yellow" : "teal"} variant="light">
      {signal === "exceeded" ? "Over allowance" : signal === "warning" ? "Pace warning" : "Below provisional leash"}
    </Badge>
  );
}

function Metric({ label, value, detail }: { label: string; value: ReactNode; detail?: string }) {
  return (
    <Stack gap={2}>
      <Text size="xs" c="dimmed" fw={600}>
        {label}
      </Text>
      <Text size="xl" fw={700}>
        {value}
      </Text>
      {detail && (
        <Text size="xs" c="dimmed">
          {detail}
        </Text>
      )}
    </Stack>
  );
}

function PurchaseCheck({ allowance }: { allowance: Allowance }) {
  const [purchase, setPurchase] = useState<number | string>("");
  const cents = typeof purchase === "number" ? Math.round(purchase * 100) : NaN;
  const valid = typeof purchase === "number" && purchase >= 0 && Number.isSafeInteger(cents);
  const available = allowance.available_minor_units;
  const projected = allowance.projected_cycle_end_minor_units;
  const after = valid && available != null ? available - cents : null;
  const projectedAfter = valid && projected != null ? projected - cents : null;
  const signal =
    after != null
      ? after <= 0
        ? "exceeded"
        : allowance.spending_signal === "warning" || (projectedAfter != null && projectedAfter < 0)
          ? "warning"
          : projectedAfter != null
            ? signalFor(after, projectedAfter)
            : null
      : null;
  const m = (value: number | null) => <Money value={value} currency={allowance.currency} />;

  return (
    <Card component="section" aria-labelledby="purchase-title" withBorder radius="lg" padding="xl">
      <Stack gap="lg">
        <div>
          <Title id="purchase-title" order={2} size="h3">
            Can I afford this?
          </Title>
          <Text c="dimmed" size="sm" mt="xs">
            Try a meal, a subscription, or another flexible purchase before buying.
          </Text>
        </div>
        <NumberInput
          label="Hypothetical flexible purchase"
          prefix="$"
          min={0}
          decimalScale={2}
          placeholder="Enter an amount"
          value={purchase}
          onChange={setPurchase}
          inputMode="decimal"
          hideControls
        />
        <Paper
          bg={
            signal === "exceeded"
              ? "var(--mantine-color-red-light)"
              : signal === "warning"
                ? "var(--mantine-color-yellow-light)"
                : "var(--mantine-color-gray-light)"
          }
          p="md"
          radius="md"
          aria-live="polite"
        >
          {after != null ? (
            <Stack gap="sm">
              <Group justify="space-between" align="flex-start" gap="sm">
                <div>
                  <Text size="sm">You would have</Text>
                  <Text size="xl" fw={700}>
                    {m(after)}
                  </Text>
                </div>
                {signal && <SignalLabel signal={signal} />}
              </Group>
              <Text size="sm">
                {projectedAfter == null ? (
                  "Pace estimate warming up; use the available balance rather than the forecast."
                ) : (
                  <>
                    At the estimated pace, {projectedAfter < 0 ? m(-projectedAfter) : m(projectedAfter)}{" "}
                    {projectedAfter < 0 ? "short" : "left"} before the next credit.
                  </>
                )}
              </Text>
              {signal && signal !== "normal" && (
                <Text size="sm" fw={600}>
                  This is a signal to pause, not a declined transaction.
                </Text>
              )}
            </Stack>
          ) : (
            <Text size="sm" c="dimmed">
              {purchase === ""
                ? "See what a purchase would do to your cushion and pace."
                : "Enter a non-negative amount in dollars and cents."}
            </Text>
          )}
        </Paper>
        <Text size="xs" c="dimmed">
          Advisory only. No bank transaction is blocked by this dashboard.
        </Text>
      </Stack>
    </Card>
  );
}

function AllowancePanel({ allowance }: { allowance: Allowance }) {
  if (allowance.status !== "active" || allowance.available_minor_units == null) {
    return (
      <Alert color="yellow" title="Allowance unavailable">
        {allowance.note || "The allowance cannot be calculated right now. Check account sync before relying on it."}
      </Alert>
    );
  }
  const m = (value: number | null | undefined) => <Money value={value} currency={allowance.currency} />;
  const available = allowance.available_minor_units;
  const projected = allowance.projected_cycle_end_minor_units;
  const signal = ["normal", "warning", "exceeded"].includes(allowance.spending_signal)
    ? (allowance.spending_signal as Signal)
    : null;
  const windows = allowance.windows_minor_units;

  return (
    <Stack gap="lg">
      <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="lg">
        <Paper
          component="section"
          aria-labelledby="allowance-title"
          bg={signal === "exceeded" ? "red.9" : signal === "warning" ? "yellow.9" : "teal.9"}
          c="white"
          radius="lg"
          p="xl"
        >
          <Stack gap="md">
            <Group justify="space-between" align="flex-start" gap="md">
              <Text id="allowance-title" size="sm" fw={700}>
                Flexible spending available
              </Text>
              {signal ? (
                <SignalLabel signal={signal} />
              ) : (
                <Badge color="gray" variant="light">
                  Pace warming up
                </Badge>
              )}
            </Group>
            <Text fz={{ base: 36, sm: 44 }} fw={700} lh={1.1} style={{ overflowWrap: "anywhere" }}>
              {m(available)}
            </Text>
            <Text size="sm" c="teal.0">
              {available <= 0 ? "You're past your available allowance." : "Available across flexible purchases."} Posted
              and pending charges are included.
            </Text>
            <Divider color="teal.6" />
            <Text size="sm">Allowance began: {time(`${allowance.activation_at}T00:00:00Z`)}</Text>
            <Text size="sm">Next credit: {time(allowance.next_credit_at)}</Text>
            <Text size="xs" c="teal.0">
              Oldest account sync: {time(allowance.last_synced_at)}. New purchases may appear later.
            </Text>
            <Text size="xs" c="teal.0">
              Adds {m(allowance.monthly_minor_units)}; unused allowance carries forward.
            </Text>
          </Stack>
        </Paper>
        <Paper component="section" aria-label="Spending pace" withBorder radius="lg" p="xl">
          <Stack gap="md">
            <Text size="sm" fw={700} c="dimmed">
              Estimated balance before next credit
            </Text>
            <Text fz={{ base: 32, sm: 38 }} fw={700} lh={1.1} style={{ overflowWrap: "anywhere" }}>
              {projected == null ? "Not enough data" : m(projected)}
            </Text>
            {projected == null && (
              <Text size="sm" c="dimmed">
                No reliable pace yet; the allowance balance above is still available.
              </Text>
            )}
            <Divider />
            <Text size="sm" fw={700}>
              Recorded flexible spending pace
            </Text>
            <Group justify="space-between" gap="sm">
              <Text size="sm">7 days</Text>
              <Text size="sm" fw={700}>
                {allowance.trailing_7_observed_daily_minor_units == null ? (
                  "Warming up"
                ) : (
                  <>{m(allowance.trailing_7_observed_daily_minor_units)} / day</>
                )}
              </Text>
            </Group>
            <Group justify="space-between" gap="sm">
              <Text size="sm">30 days</Text>
              <Text size="sm" fw={700}>
                {allowance.trailing_30_observed_daily_minor_units == null ? (
                  "Warming up"
                ) : (
                  <>{m(allowance.trailing_30_observed_daily_minor_units)} / day</>
                )}
              </Text>
            </Group>
            <Text size="sm" c="dimmed">
              Provisional leash ~{m(Math.round((allowance.monthly_minor_units * 12) / 365.2425))} / day. This is
              spending capacity, not a sustainability target.
            </Text>
            <Text size="xs" c="dimmed">
              Positive recorded purchases, including history before activation; unmatched purchases count as flexible.
              Earlier purchases inform pace but do not reduce available allowance. Plaid data may lag.
            </Text>
            {allowance.trailing_7_unmatched_count != null && allowance.trailing_7_unmatched_count > 0 && (
              <Text size="xs" c="dimmed">
                7d unmatched {allowance.trailing_7_unmatched_count} ({m(allowance.trailing_7_unmatched_minor_units)}) ·
                counted as flexible.
              </Text>
            )}
            <Divider />
            <Group justify="space-between" gap="sm">
              <Text size="sm">Pace used for estimate</Text>
              <Text size="sm" fw={700}>
                {allowance.trailing_7_daily_minor_units == null ? (
                  "Warming up"
                ) : (
                  <>{m(allowance.trailing_7_daily_minor_units)} / day</>
                )}
              </Text>
            </Group>
            <Text size="xs" c="dimmed">
              Uses positive flexible purchases over the last seven days, including before the allowance began; early
              post-start bursts can increase the pace. Earlier purchases inform the estimate but do not reduce your
              available balance. Plaid data may lag.
            </Text>
            {allowance.estimated_exhaustion_at && (
              <Text size="sm">
                Without future credits, this pace would use up the cushion around{" "}
                <strong>{time(allowance.estimated_exhaustion_at)}</strong>.
              </Text>
            )}
          </Stack>
        </Paper>
      </SimpleGrid>
      <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="lg">
        <PurchaseCheck allowance={allowance} />
        <Card component="section" aria-labelledby="cycle-title" withBorder radius="lg" padding="xl">
          <Stack gap="lg">
            <div>
              <Title id="cycle-title" order={2} size="h3">
                Where you stand
              </Title>
              <Text c="dimmed" size="sm" mt="xs">
                Your allowance rolls forward; it doesn't reset at month-end.
              </Text>
            </div>
            <Paper bg="var(--mantine-color-gray-light)" p="md" radius="md">
              <Stack gap="sm">
                <Group justify="space-between" gap="sm">
                  <Text size="sm">Monthly credit</Text>
                  <Text size="sm" fw={700}>
                    {m(allowance.monthly_minor_units)}
                  </Text>
                </Group>
                <Group justify="space-between" gap="sm">
                  <Text size="sm">Carried from earlier</Text>
                  <Text size="sm" fw={700}>
                    {m(allowance.prior_carry_minor_units)}
                  </Text>
                </Group>
                <Divider />
                <Group justify="space-between" gap="sm">
                  <Text size="sm">Spent this cycle</Text>
                  <Text size="sm" fw={700}>
                    - {m(windows?.current_credit_cycle_minor_units)}
                  </Text>
                </Group>
              </Stack>
            </Paper>
            <Text size="sm" c="dimmed">
              {m(allowance.pending_minor_units)} pending is included in the balance.
            </Text>
            {allowance.review_transaction_count > 0 && (
              <Alert
                color="yellow"
                title={
                  <>
                    {allowance.review_transaction_count}{" "}
                    {allowance.review_transaction_count === 1 ? "charge" : "charges"} ({m(allowance.review_minor_units)}
                    ) need review
                  </>
                }
                variant="light"
              >
                Counted as flexible until classified; the cushion could change.
              </Alert>
            )}
          </Stack>
        </Card>
      </SimpleGrid>
      <Accordion variant="contained" radius="md">
        <Accordion.Item value="history">
          <Accordion.Control>Explore spending history &amp; calculation details</Accordion.Control>
          <Accordion.Panel>
            <Stack gap="lg">
              <Text size="sm" c="dimmed">
                These are overlapping views of the same purchases, not separate budgets. Your available balance includes
                all credits since activation, less posted and pending flexible spending.
              </Text>
              <SimpleGrid cols={{ base: 2, sm: 4 }} spacing="lg">
                <Metric
                  label="LAST 7 DAYS"
                  value={m(windows?.trailing_7_days_minor_units)}
                  detail="Since allowance start"
                />
                <Metric
                  label="LAST 30 DAYS"
                  value={m(windows?.trailing_30_days_minor_units)}
                  detail="Since allowance start"
                />
                <Metric
                  label="CALENDAR MONTH"
                  value={m(windows?.calendar_month_minor_units)}
                  detail="Since activation"
                />
                <Metric label="THIS YEAR" value={m(windows?.year_to_date_minor_units)} detail="Since activation" />
              </SimpleGrid>
              <Text size="sm" c="dimmed">
                {m(allowance.unmatched_refunds_minor_units)} of unlinked refunds are excluded from the balance. Oldest
                account sync: {time(allowance.last_synced_at)}. Plaid data can lag or be misclassified.
              </Text>
            </Stack>
          </Accordion.Panel>
        </Accordion.Item>
      </Accordion>
    </Stack>
  );
}

function SpendCard({ card }: { card: CardView }) {
  const alert =
    card.alert_state === "exceeded"
      ? "Limit exceeded"
      : card.alert_state === "warning"
        ? "Near limit"
        : !card.statement_available && card.cycle_start
          ? "First statement pending"
          : card.alert_state === "unavailable"
            ? "Unavailable"
            : "Within limit";
  return (
    <Card component="article" withBorder radius="md" padding="lg">
      <Stack gap="sm">
        <Group justify="space-between" align="flex-start" gap="sm">
          <div>
            <Text fw={650}>{cardTitle(card)}</Text>
            <Text c="dimmed" size="sm">
              {card.institution_name}
            </Text>
          </div>
          <Badge
            color={card.alert_state === "exceeded" ? "red" : card.alert_state === "warning" ? "yellow" : "gray"}
            variant="light"
          >
            {alert}
          </Badge>
        </Group>
        <Text size="xl" fw={700} mt="sm">
          <Money value={card.spend_minor_units} currency={card.currency} />
        </Text>
        <Text size="sm" c="dimmed">
          {card.statement_available ? (
            <>
              This statement
              {card.limit_minor_units != null && (
                <>
                  {" "}
                  · <Money value={card.limit_minor_units} currency={card.currency} /> card limit
                </>
              )}
            </>
          ) : card.cycle_start ? (
            `Provisional card total since ${card.cycle_start}; statement date not yet reported. Includes purchases outside the allowance.`
          ) : (
            "Statement data unavailable"
          )}
        </Text>
        <Text size="xs" c="dimmed">
          Pending <Money value={card.pending_minor_units} currency={card.currency} /> · Last synced{" "}
          {time(card.last_synced_at)}
        </Text>
      </Stack>
    </Card>
  );
}

function ruleConditionText(condition: RuleCondition): string {
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

function ConfigurationPanel({
  configuration,
  loading,
  error,
}: {
  configuration: SpendConfiguration | null;
  loading: boolean;
  error: string | null;
}) {
  const allowance = configuration?.allowance;
  return (
    <Stack gap="lg">
      <Group justify="space-between" align="flex-start" gap="md">
        <div>
          <Title order={2} size="h3">
            Configuration
          </Title>
          <Text size="sm" c="dimmed" mt="xs">
            Read-only settings currently loaded by Plaid Spend. Account IDs are omitted; changes are made in the private
            configuration and take effect after rollout.
          </Text>
        </div>
        <Badge color="gray" variant="light">
          Read only
        </Badge>
      </Group>
      {error && (
        <Alert color="red" title="Couldn't load configuration">
          {error}
        </Alert>
      )}
      {loading && !configuration && (
        <Text size="sm" c="dimmed">
          Loading configuration…
        </Text>
      )}
      {configuration && (
        <>
          {allowance ? (
            <Card component="section" aria-labelledby="configuration-allowance-title" withBorder radius="lg" p="lg">
              <Stack gap="md">
                <Title id="configuration-allowance-title" order={3} size="h4">
                  Flexible allowance policy
                </Title>
                <SimpleGrid cols={{ base: 1, sm: 2, md: 4 }} spacing="md">
                  <Metric
                    label="MONTHLY ALLOWANCE"
                    value={<Money value={allowance.monthly_minor_units} currency={allowance.currency} />}
                  />
                  <Metric label="START DATE" value={allowance.activation_at} />
                  <Metric label="ACCOUNTS IN SCOPE" value={allowance.spending_account_count} />
                  <Metric label="MAX SYNC AGE" value={`${allowance.max_sync_age_hours} hours`} />
                </SimpleGrid>
                <Divider />
                <div>
                  <Text fw={650} mb="sm">
                    Classification rules
                  </Text>
                  <Stack gap="xs">
                    {allowance.rules.map((rule, index) => {
                      const { label, color } = ruleKindDisplay[rule.kind];
                      return (
                        <Paper key={`${rule.kind}-${index}`} withBorder radius="md" p="sm">
                          <Group align="flex-start" gap="sm" wrap="nowrap">
                            <Text size="sm" c="dimmed" w={20} ta="right">
                              {index + 1}.
                            </Text>
                            <Badge color={color} variant="light" style={{ flexShrink: 0 }}>
                              {label}
                            </Badge>
                            <Stack gap={2} miw={0}>
                              <Text size="sm" style={{ overflowWrap: "anywhere" }}>
                                {ruleConditionText(rule.condition)}
                              </Text>
                              {rule.description && (
                                <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                                  {rule.description}
                                </Text>
                              )}
                            </Stack>
                          </Group>
                        </Paper>
                      );
                    })}
                  </Stack>
                  <Text size="xs" c="dimmed" mt="sm">
                    Rules are checked in order; the first matching rule applies. Unmatched purchases count as flexible.
                  </Text>
                </div>
              </Stack>
            </Card>
          ) : (
            <Alert color="yellow" title="No flexible allowance configured">
              Card settings are still shown below.
            </Alert>
          )}
          <section aria-labelledby="configuration-cards-title">
            <Group justify="space-between" align="baseline">
              <Title id="configuration-cards-title" order={3} size="h4">
                Card settings
              </Title>
              <Text size="sm" c="dimmed">
                {configuration.cards.length} configured
              </Text>
            </Group>
            <Text size="xs" c="dimmed" mt="xs" mb="md">
              Limits are shown in minor units of each account's currency.
            </Text>
            <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="md">
              {configuration.cards.map((card, index) => (
                <Card key={`${card.label}-${index}`} withBorder radius="md" p="lg">
                  <Stack gap="sm">
                    <Group justify="space-between" align="flex-start" gap="sm">
                      <Text fw={650} style={{ overflowWrap: "anywhere" }}>
                        {card.label}
                      </Text>
                      <Badge color={card.enabled ? "teal" : "gray"} variant="light">
                        {card.enabled ? "Enabled" : "Disabled"}
                      </Badge>
                    </Group>
                    <Text size="sm">
                      Card limit: {card.limit_minor_units == null ? "Not set" : card.limit_minor_units.toLocaleString()}
                    </Text>
                    <Text size="sm" c="dimmed">
                      Alert threshold:{" "}
                      {card.alert_threshold_percent == null ? "Not set" : `${card.alert_threshold_percent}%`}
                    </Text>
                  </Stack>
                </Card>
              ))}
            </SimpleGrid>
            {!configuration.cards.length && (
              <Text size="sm" c="dimmed" mt="sm">
                No cards configured.
              </Text>
            )}
          </section>
        </>
      )}
    </Stack>
  );
}

const dispositionText = {
  counted: "Counted in allowance",
  pace_only: "Pace only · before allowance start",
  fixed: "Mandatory · outside allowance",
  excluded: "Excluded from allowance",
  held_refund: "Refund held for review",
  superseded_pending: "Pending version replaced by posted charge",
  other_currency: "Different currency · outside allowance",
} satisfies Record<NonNullable<TransactionRow["disposition"]>, string>;

const statementText = {
  counted: "Counted in card cycle",
  outside_cycle: "Outside current card cycle",
  superseded_pending: "Pending version replaced by posted charge",
  card_payment: "Card payment excluded from card spend",
  other_currency: "Different currency excluded from card spend",
  unavailable: "Card cycle unavailable",
} satisfies Record<NonNullable<TransactionRow["statement_reason"]>, string>;

function isReviewRow(row: TransactionRow): boolean {
  return (
    row.allowance_in_scope &&
    ((row.disposition === "counted" && (row.rule == null || row.rule.kind === "review")) ||
      row.disposition === "held_refund")
  );
}

function classificationForRow(row: TransactionRow): { label: string; color: string } {
  if (row.disposition === "held_refund") return { label: "Refund held", color: "orange" };
  if (row.disposition === "superseded_pending") return { label: "Superseded", color: "gray" };
  if (row.disposition === "other_currency") return { label: "Other currency", color: "gray" };
  if (row.rule) return ruleKindDisplay[row.rule.kind];
  if (!row.allowance_in_scope) return { label: "Outside allowance", color: "gray" };
  if (row.disposition === null) return { label: "Unavailable", color: "gray" };
  return { label: "Unmatched", color: "orange" };
}

function categoryForRow(row: TransactionRow): string {
  return row.analysis_category_label || row.rule?.analysis_category || "No category inferred";
}

function CompactCounterparties({ counterparties }: { counterparties: TransactionRow["counterparties"] }) {
  if (!counterparties?.length) return null;
  return (
    <Text size="xs" c="dimmed" lineClamp={1}>
      Counterparties: {counterparties.map((counterparty) => counterparty.name || "Unnamed").join(", ")}
    </Text>
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

function PlaidSourceFields({ row }: { row: TransactionRow }) {
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

function TransactionDetails({ row, currency }: { row: TransactionRow; currency: string }) {
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
      {row.rule && (
        <Stack gap={2}>
          <Text size="sm">
            <strong>Rule #{row.rule_number}:</strong> {ruleConditionText(row.rule.condition)}
          </Text>
          {row.rule.description && (
            <Text size="sm" c="dimmed">
              {row.rule.description}
            </Text>
          )}
          {row.rule.analysis_category && (
            <Text size="xs" c="dimmed">
              Analysis category: {categoryForRow(row)}
              {row.analysis_category_label && ` (${row.rule.analysis_category})`}
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
        <Text size="sm">7-day pace input: {m(row.trailing_7_pace_minor_units)}</Text>
        <Text size="sm">30-day pace input: {m(row.trailing_30_pace_minor_units)}</Text>
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

function TransactionsPanel({
  transactions,
  loading,
  error,
  window,
  onWindowChange,
}: {
  transactions: TransactionsView | null;
  loading: boolean;
  error: string | null;
  window: TransactionWindow;
  onWindowChange: (window: TransactionWindow) => void;
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
  const reviewRows = rows.filter(isReviewRow);
  const netAllowance = rows.reduce((sum, row) => sum + row.allowance_minor_units, 0);

  return (
    <Stack gap="lg">
      <Group justify="space-between" align="flex-start" gap="md">
        <div>
          <Title order={1} size="h3">
            Transactions
          </Title>
          <Text size="sm" c="dimmed" mt="xs">
            Recent Plaid transactions and the decisions behind your allowance and card totals. Positive amounts are
            charges; negative amounts are credits.
          </Text>
        </div>
        <Badge color="gray" variant="light">
          Read only
        </Badge>
      </Group>
      <Group justify="space-between" align="end" gap="md">
        <SegmentedControl
          aria-label="Transaction period"
          value={window}
          onChange={(value) => onWindowChange(value as TransactionWindow)}
          data={[
            { label: "7 days", value: "7d" },
            { label: "30 days", value: "30d" },
            { label: "Credit cycle", value: "cycle" },
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
          {window === "cycle" && allowance?.status !== "active" && (
            <Text size="sm" c="dimmed">
              Showing the last 30 days because the credit cycle is unavailable.
            </Text>
          )}
          <Paper withBorder radius="lg" p="lg">
            <Stack gap="md">
              <SimpleGrid cols={{ base: 1, sm: 3 }} spacing="md">
                <Metric
                  label="TRANSACTIONS IN PERIOD"
                  value={rows.length}
                  detail={`Since ${transactions.window_start}`}
                />
                <Metric
                  label="NET ALLOWANCE SPEND"
                  value={allowance?.status === "active" ? m(netAllowance) : "Unavailable"}
                  detail="Positive charges less accepted refunds"
                />
                <Metric
                  label="NEEDS CLASSIFICATION"
                  value={
                    allowance?.status === "active"
                      ? `${reviewRows.length} · ${money(
                          reviewRows.reduce((sum, row) => sum + Math.max(0, row.allowance_minor_units), 0),
                          currency
                        )}`
                      : "Unavailable"
                  }
                  detail="Unmatched charges count; held refunds do not"
                />
              </SimpleGrid>
              {window === "cycle" && allowance?.status === "active" && (
                <>
                  <Divider />
                  <Text size="sm" fw={650}>
                    Allowance bridge
                  </Text>
                  <Text size="sm">
                    {m(allowance.prior_carry_minor_units)} carried + {m(allowance.monthly_minor_units)} monthly credit −{" "}
                    {m(allowance.windows_minor_units?.current_credit_cycle_minor_units)} cycle spend ={" "}
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
              <Table miw={850} verticalSpacing="sm" horizontalSpacing="md" striped highlightOnHover withTableBorder>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Date</Table.Th>
                    <Table.Th>Merchant</Table.Th>
                    <Table.Th>Account</Table.Th>
                    <Table.Th>Category</Table.Th>
                    <Table.Th ta="right">Amount</Table.Th>
                    <Table.Th ta="right">Allowance</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {shown.map(({ row, index }) => {
                    const classification = classificationForRow(row);
                    const expanded = expandedRow === index;
                    return (
                      <Fragment key={`${row.date}-${row.account_label}-${index}`}>
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
                              <Text component="span" size="sm" c="dimmed" aria-hidden="true">
                                {expanded ? "▾" : "▸"}
                              </Text>
                              <Stack gap={0} miw={0}>
                                <Text size="sm" fw={650} style={{ overflowWrap: "anywhere" }}>
                                  {row.merchant_name || row.name}
                                </Text>
                                <CompactCounterparties counterparties={row.counterparties} />
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
                              <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
                                {categoryForRow(row)}
                              </Text>
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
                      </Fragment>
                    );
                  })}
                </Table.Tbody>
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
                        <Stack gap={2} miw={0}>
                          <Text fw={650} size="sm" style={{ overflowWrap: "anywhere" }}>
                            {row.merchant_name || row.name}
                          </Text>
                          <CompactCounterparties counterparties={row.counterparties} />
                          <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                            {categoryForRow(row)}
                          </Text>
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

function App() {
  const [view, setView] = useState<View | null>(null);
  const [state, setState] = useState("Connecting");
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<SpendTab>(() => tabForHash(window.location.hash));
  const [configuration, setConfiguration] = useState<SpendConfiguration | null>(null);
  const [configurationLoading, setConfigurationLoading] = useState(false);
  const [configurationError, setConfigurationError] = useState<string | null>(null);
  const [transactions, setTransactions] = useState<TransactionsView | null>(null);
  const [transactionWindow, setTransactionWindow] = useState<TransactionWindow>("30d");
  const [transactionsLoading, setTransactionsLoading] = useState(false);
  const [transactionsError, setTransactionsError] = useState<string | null>(null);
  const [viewRevision, setViewRevision] = useState(0);
  useEffect(() => {
    if (!window.location.hash) window.history.replaceState(null, "", "#/spending");
    const updateTab = () => setActiveTab(tabForHash(window.location.hash));
    window.addEventListener("hashchange", updateTab);
    return () => window.removeEventListener("hashchange", updateTab);
  }, []);
  useEffect(() => {
    let mounted = true;
    const load = async () => {
      try {
        const response = await fetch("/api/v1/view", { cache: "no-store", credentials: "same-origin" });
        if (response.status === 401) {
          window.location.assign("/auth/login");
          return;
        }
        if (!response.ok) throw new Error(`View request returned ${response.status}`);
        const data: View = await response.json();
        if (mounted) {
          setView(data);
          setError(null);
        }
      } catch (cause) {
        if (mounted) {
          setError(cause instanceof Error ? cause.message : "View request failed");
          setState("Unable to refresh");
        }
      }
    };
    void load();
    const events = new EventSource("/api/v1/events");
    events.addEventListener("view", (event) => {
      try {
        if (mounted) {
          setView(JSON.parse(event.data));
          setError(null);
          setState("Live updates");
          setViewRevision((revision) => revision + 1);
        }
      } catch (cause) {
        if (mounted) setError(cause instanceof Error ? cause.message : "Could not read live update");
      }
    });
    events.onerror = () => {
      if (mounted) {
        setState("Reconnecting");
        void load();
      }
    };
    return () => {
      mounted = false;
      events.close();
    };
  }, []);
  useEffect(() => {
    if (activeTab !== "configuration" || configuration != null) return;
    let mounted = true;
    const load = async () => {
      setConfigurationLoading(true);
      setConfigurationError(null);
      try {
        const response = await fetch("/api/v1/configuration", {
          cache: "no-store",
          credentials: "same-origin",
        });
        if (response.status === 401) {
          window.location.assign("/auth/login");
          return;
        }
        if (!response.ok) throw new Error(`Configuration request returned ${response.status}`);
        const data: SpendConfiguration = await response.json();
        if (mounted) setConfiguration(data);
      } catch (cause) {
        if (mounted) setConfigurationError(cause instanceof Error ? cause.message : "Configuration request failed");
      } finally {
        if (mounted) setConfigurationLoading(false);
      }
    };
    void load();
    return () => {
      mounted = false;
    };
  }, [activeTab, configuration]);
  useEffect(() => {
    if (activeTab !== "transactions") return;
    const controller = new AbortController();
    const load = async () => {
      setTransactionsLoading(true);
      setTransactionsError(null);
      try {
        const response = await fetch(`/api/v1/transactions?window=${transactionWindow}`, {
          cache: "no-store",
          credentials: "same-origin",
          signal: controller.signal,
        });
        if (response.status === 401) {
          window.location.assign("/auth/login");
          return;
        }
        if (!response.ok) throw new Error(`Transactions request returned ${response.status}`);
        const data: TransactionsView = await response.json();
        if (!controller.signal.aborted) setTransactions(data);
      } catch (cause) {
        if (!controller.signal.aborted) {
          setTransactionsError(cause instanceof Error ? cause.message : "Transactions request failed");
        }
      } finally {
        if (!controller.signal.aborted) setTransactionsLoading(false);
      }
    };
    void load();
    return () => controller.abort();
  }, [activeTab, transactionWindow, viewRevision]);
  const cards = view?.cards || [];
  return (
    <MantineProvider defaultColorScheme="auto">
      <Tabs
        value={activeTab}
        onChange={(tab) => {
          if (tab) window.location.hash = `/${tab}`;
        }}
        keepMounted={false}
        variant="pills"
        color="teal"
      >
        <Paper component="header" radius={0} withBorder>
          <Container size="lg" py="xs">
            <Grid align="center" gap="xs">
              <Grid.Col span={{ base: 6, xs: 4 }} order={1}>
                <Anchor href="#/spending" size="lg" fw={700} c="var(--mantine-color-text)" underline="never">
                  Spend
                </Anchor>
              </Grid.Col>
              <Grid.Col span={{ base: 12, xs: 4 }} order={{ base: 3, xs: 2 }}>
                <ScrollArea type="auto" scrollbars="x" w="100%">
                  <Center>
                    <Tabs.List aria-label="Spend pages" miw="max-content">
                      <Tabs.Tab value="spending" aria-label="Spending">
                        <Text span visibleFrom="xs">
                          Spending
                        </Text>
                        <Text span hiddenFrom="xs">
                          Spend
                        </Text>
                      </Tabs.Tab>
                      <Tabs.Tab value="transactions">Transactions</Tabs.Tab>
                      <Tabs.Tab value="configuration" aria-label="Configuration">
                        <Text span visibleFrom="xs">
                          Configuration
                        </Text>
                        <Text span hiddenFrom="xs">
                          Config
                        </Text>
                      </Tabs.Tab>
                    </Tabs.List>
                  </Center>
                </ScrollArea>
              </Grid.Col>
              <Grid.Col span={{ base: 6, xs: 4 }} order={{ base: 2, xs: 3 }}>
                <Group justify="flex-end">
                  <form action="/auth/logout" method="post">
                    <Button type="submit" variant="subtle" color="gray" size="sm">
                      Sign out
                    </Button>
                  </form>
                </Group>
              </Grid.Col>
            </Grid>
          </Container>
        </Paper>
        <Container component="main" size="lg" py="xl">
          <Tabs.Panel value="spending">
            <Stack gap="xl">
              <Group justify="space-between" gap="md">
                <Title order={1} size="h3">
                  Flexible spending
                </Title>
                <Badge color={state === "Live updates" ? "teal" : "gray"} variant="dot" aria-live="polite">
                  {state}
                </Badge>
              </Group>
              {error && (
                <Alert color="red" title="Couldn't refresh your view">
                  {error}. Showing the most recent data we have.
                </Alert>
              )}
              {view?.allowance ? (
                <AllowancePanel allowance={view.allowance} />
              ) : (
                <Alert color="yellow" title="No flexible allowance yet">
                  {view
                    ? "No allowance is configured. Card totals below are not a flexible-spend budget."
                    : "Loading your spending picture…"}
                </Alert>
              )}
              <section aria-labelledby="cards-title">
                <Group justify="space-between" align="baseline">
                  <Title order={2} id="cards-title" size="h3">
                    Card statements
                  </Title>
                  <Text size="sm" c="dimmed">
                    {cards.length} {cards.length === 1 ? "card" : "cards"}
                  </Text>
                </Group>
                <Text size="sm" c="dimmed" mt="xs" mb="md">
                  Statement cycles and card limits are not a flexible spending budget.
                </Text>
                <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="md">
                  {cards.map((card, index) => (
                    <SpendCard card={card} key={`${card.account_name}-${index}`} />
                  ))}
                </SimpleGrid>
                {view && !cards.length && (
                  <Text size="sm" c="dimmed">
                    No cards configured.
                  </Text>
                )}
              </section>
              <Divider />
              <Group justify="space-between" gap="sm">
                <Text size="xs" c="dimmed">
                  Advisory estimates, not bank controls. Review your accounts for decisions that matter.
                </Text>
                <Text size="xs" c="dimmed">
                  View updated {time(view?.generated_at)}
                </Text>
              </Group>
            </Stack>
          </Tabs.Panel>
          <Tabs.Panel value="transactions">
            <TransactionsPanel
              transactions={transactions?.window === transactionWindow ? transactions : null}
              loading={transactionsLoading}
              error={transactionsError}
              window={transactionWindow}
              onWindowChange={setTransactionWindow}
            />
          </Tabs.Panel>
          <Tabs.Panel value="configuration">
            <ConfigurationPanel
              configuration={configuration}
              loading={configurationLoading}
              error={configurationError}
            />
          </Tabs.Panel>
        </Container>
      </Tabs>
    </MantineProvider>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
