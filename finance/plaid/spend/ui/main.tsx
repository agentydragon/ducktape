import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { createRoot } from "react-dom/client";
import {
  Accordion,
  Alert,
  Anchor,
  Badge,
  Button,
  Card,
  Container,
  Divider,
  Group,
  MantineProvider,
  NumberInput,
  Paper,
  SimpleGrid,
  Stack,
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
type RuleCondition = components["schemas"]["Rule"]["condition"];
type RuleKind = components["schemas"]["Rule"]["kind"];

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
          bg={signal === "exceeded" ? "red.0" : signal === "warning" ? "yellow.0" : "gray.0"}
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
            <Paper bg="gray.0" p="md" radius="md">
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
                            <Stack gap={2} style={{ minWidth: 0 }}>
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

function App() {
  const [view, setView] = useState<View | null>(null);
  const [state, setState] = useState("Connecting");
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<string | null>("spending");
  const [configuration, setConfiguration] = useState<SpendConfiguration | null>(null);
  const [configurationLoading, setConfigurationLoading] = useState(false);
  const [configurationError, setConfigurationError] = useState<string | null>(null);
  useEffect(() => {
    let mounted = true;
    const load = async () => {
      try {
        const response = await fetch("/api/v1/web/view", { cache: "no-store", credentials: "same-origin" });
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
    const events = new EventSource("/api/v1/web/events");
    events.addEventListener("view", (event) => {
      try {
        if (mounted) {
          setView(JSON.parse(event.data));
          setError(null);
          setState("Live updates");
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
        const response = await fetch("/api/v1/web/configuration", {
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
  const cards = view?.cards || [];
  return (
    <MantineProvider defaultColorScheme="light">
      <Paper component="header" radius={0} withBorder>
        <Container size="lg" py="sm">
          <Group justify="space-between">
            <Anchor href="/" size="lg" fw={700} c="teal.9" underline="never">
              Spend
            </Anchor>
            <form action="/auth/logout" method="post">
              <Button type="submit" variant="subtle" color="gray" size="sm">
                Sign out
              </Button>
            </form>
          </Group>
        </Container>
      </Paper>
      <Container component="main" size="lg" py="xl">
        <Tabs value={activeTab} onChange={setActiveTab} keepMounted={false}>
          <Tabs.List aria-label="Spend pages">
            <Tabs.Tab value="spending">Spending</Tabs.Tab>
            <Tabs.Tab value="configuration">Configuration</Tabs.Tab>
          </Tabs.List>
          <Tabs.Panel value="spending" pt="md">
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
          <Tabs.Panel value="configuration" pt="md">
            <ConfigurationPanel
              configuration={configuration}
              loading={configurationLoading}
              error={configurationError}
            />
          </Tabs.Panel>
        </Tabs>
      </Container>
    </MantineProvider>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
