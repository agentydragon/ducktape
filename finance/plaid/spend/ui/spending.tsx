import {
  Accordion,
  Alert,
  Badge,
  Card,
  Divider,
  Group,
  NumberInput,
  Paper,
  SimpleGrid,
  Skeleton,
  Stack,
  Text,
  Title,
} from "@mantine/core";

import type { AllowanceView, CardView, SpendTransactionsView, SpendView } from "./schema";
import { HelpTooltip } from "../../../../util/frontend/help_tooltip";

import { Metric, Money, SignalLabel, time, periodLabels, type Signal } from "./spend_display";
import { SpendingHistoryChart } from "./SpendingHistoryChart";
import type { AllowancePeriodId, EstimatePeriodId } from "./types";

function cardTitle(card: CardView): string {
  return `${card.label || card.account_name || "Card"}${card.mask ? ` ···· ${card.mask}` : ""}`;
}
function spendPeriod(allowance: AllowanceView, periodId: AllowancePeriodId) {
  return allowance.spend_periods.find((report) => report.period.id === periodId);
}
function pacePeriod(allowance: AllowanceView, periodId: AllowancePeriodId) {
  return allowance.recorded_pace_periods.find((report) => report.period.id === periodId);
}
function signalFor(available: number, projected: number): Signal {
  return available <= 0 ? "exceeded" : projected < 0 ? "warning" : "normal";
}

type EstimateState = "current" | "loading" | "unavailable";

function PurchaseCheck({
  allowance,
  purchase,
  onPurchaseChange,
  estimateState,
  estimatePeriodId,
}: {
  allowance: AllowanceView;
  purchase: number | string;
  onPurchaseChange: (value: number | string) => void;
  estimateState: EstimateState;
  estimatePeriodId: EstimatePeriodId;
}) {
  const cents = typeof purchase === "number" ? Math.round(purchase * 100) : NaN;
  const valid = typeof purchase === "number" && purchase >= 0 && Number.isSafeInteger(cents);
  const available = allowance.available_minor_units;
  const projected = estimateState === "current" ? allowance.forecast.projected_cycle_end_minor_units : null;
  const after = valid && available != null ? available - cents : null;
  const projectedAfter = valid && projected != null ? projected - cents : null;
  const signal =
    after != null
      ? after <= 0
        ? "exceeded"
        : estimateState === "current" &&
            (allowance.spending_signal === "warning" || (projectedAfter != null && projectedAfter < 0))
          ? "warning"
          : estimateState === "current" && projectedAfter != null
            ? signalFor(after, projectedAfter)
            : null
      : null;
  const m = (value: number | null) => <Money value={value} currency={allowance.currency} />;

  return (
    <Card component="section" aria-labelledby="purchase-title" withBorder radius="lg" padding="xl">
      <Stack gap="lg">
        <Group gap="xs">
          <Title id="purchase-title" order={2} size="h3">
            Can I afford this?
          </Title>
          <HelpTooltip
            topic="Purchase check"
            description="Try a flexible purchase before buying. The amount is subtracted from your available allowance and projected balance; this does not make a purchase."
          />
        </Group>
        <NumberInput
          label="Hypothetical flexible purchase"
          prefix="$"
          min={0}
          decimalScale={2}
          placeholder="Enter an amount"
          value={purchase}
          onChange={onPurchaseChange}
          inputMode="decimal"
          hideControls
        />
        <Paper
          bg={
            estimateState !== "current"
              ? "var(--mantine-color-gray-light)"
              : signal === "exceeded"
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
                {estimateState === "loading" && after > 0 ? (
                  <Skeleton height={20} width={112} radius="xl" />
                ) : (
                  signal && <SignalLabel signal={signal} />
                )}
              </Group>
              <Text size="sm">
                {estimateState === "loading" ? (
                  <>
                    At the estimated pace, <Skeleton component="span" height={12} width={88} display="inline-block" />{" "}
                    before the next credit.
                  </>
                ) : estimateState === "unavailable" ? (
                  `No ${periodLabels[estimatePeriodId].toLowerCase()} estimate is available; the allowance balance above is unchanged.`
                ) : projectedAfter == null ? (
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
      </Stack>
    </Card>
  );
}

function AllowancePanel({
  allowance,
  purchase,
  onPurchaseChange,
  estimateState,
  estimatePeriodId,
}: {
  allowance: AllowanceView;
  purchase: number | string;
  onPurchaseChange: (value: number | string) => void;
  estimateState: EstimateState;
  estimatePeriodId: EstimatePeriodId;
}) {
  if (allowance.status !== "active" || allowance.available_minor_units == null) {
    return (
      <Alert color="yellow" title="Allowance unavailable">
        {allowance.note || "The allowance cannot be calculated right now. Check account sync before relying on it."}
      </Alert>
    );
  }
  const m = (value: number | null | undefined) => <Money value={value} currency={allowance.currency} />;
  const available = allowance.available_minor_units;
  const currentEstimate = estimateState === "current";
  const projected = currentEstimate ? allowance.forecast.projected_cycle_end_minor_units : null;
  const signal =
    currentEstimate && ["normal", "warning", "exceeded"].includes(allowance.spending_signal)
      ? (allowance.spending_signal as Signal)
      : null;
  const cycleSpend = spendPeriod(allowance, "credit_cycle");
  const selectedPace = currentEstimate ? pacePeriod(allowance, allowance.forecast.basis_period.id) : undefined;
  const unmatched = selectedPace?.unmatched_charges;
  const historyPeriods = [
    { id: "rolling_7d", label: "LAST 7 DAYS" },
    { id: "rolling_30d", label: "LAST 30 DAYS" },
    { id: "calendar_month", label: "CALENDAR MONTH" },
    { id: "year_to_date", label: "THIS YEAR" },
  ] as const;

  return (
    <Stack gap="lg">
      <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="lg">
        <Paper
          component="section"
          aria-labelledby="allowance-title"
          bg={
            estimateState !== "current"
              ? "gray.8"
              : signal === "exceeded"
                ? "red.9"
                : signal === "warning"
                  ? "yellow.9"
                  : "teal.9"
          }
          c="white"
          radius="lg"
          p="xl"
        >
          <Stack gap="md">
            <Group justify="space-between" align="flex-start" gap="md">
              <Group gap="xs">
                <Text id="allowance-title" size="sm" fw={700}>
                  Flexible spending available
                </Text>
                <HelpTooltip
                  topic="Available allowance"
                  description="All allowance credits since activation, less posted and pending flexible purchases. Unused allowance carries forward. This is a spending allowance, not a sustainability target."
                />
              </Group>
              {estimateState === "loading" ? (
                <Skeleton height={20} width={112} radius="xl" />
              ) : estimateState === "unavailable" ? (
                <Badge color="gray" variant="light">
                  Estimate unavailable
                </Badge>
              ) : signal ? (
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
              {available <= 0 ? "You're past your available allowance." : "Includes posted and pending purchases."}
            </Text>
            <Divider color="teal.6" />
            <Text size="sm">Next credit: {time(allowance.next_credit_at)}</Text>
            <Text size="xs" c="teal.0">
              Oldest account sync: {time(allowance.last_synced_at)}.
            </Text>
          </Stack>
        </Paper>
        <Paper component="section" aria-label="Spending pace" withBorder radius="lg" p="xl">
          <Stack gap="md">
            <Group gap="xs">
              <Text size="sm" fw={700} c="dimmed">
                Estimated balance before next credit
              </Text>
              <HelpTooltip
                topic="Estimated balance"
                description="Projects the available allowance using the selected spending window. A higher daily pace may be used when purchases since activation are concentrated in fewer days. Plaid purchases can arrive late."
              />
            </Group>
            <Text size="xs" c="dimmed">
              {estimateState === "current"
                ? `Based on ${periodLabels[estimatePeriodId].toLowerCase()}`
                : estimateState === "loading"
                  ? `Loading ${periodLabels[estimatePeriodId].toLowerCase()} estimate`
                  : `${periodLabels[estimatePeriodId]} estimate unavailable`}
            </Text>
            {estimateState === "loading" ? (
              <Skeleton height={42} width={180} />
            ) : estimateState === "unavailable" ? (
              <Text size="sm" c="dimmed">
                No estimate loaded for {periodLabels[estimatePeriodId].toLowerCase()}.
              </Text>
            ) : (
              <Text fz={{ base: 32, sm: 38 }} fw={700} lh={1.1} style={{ overflowWrap: "anywhere" }}>
                {projected == null ? "Not enough data" : m(projected)}
              </Text>
            )}
            {projected == null && estimateState === "current" && (
              <Text size="sm" c="dimmed">
                No reliable pace yet; the allowance balance above is still available.
              </Text>
            )}
            <Divider />
            <Group justify="space-between" gap="sm">
              <Group gap="xs">
                <Text size="sm" fw={700}>
                  Recorded flexible spending pace
                </Text>
                <HelpTooltip
                  topic="Recorded pace"
                  description="Average positive flexible purchases in the selected window, including unmatched purchases and history before activation. Earlier purchases inform pace but do not reduce the available allowance."
                />
              </Group>
              <Text size="sm" fw={700}>
                {estimateState === "loading" ? (
                  <Skeleton height={18} width={100} />
                ) : estimateState === "unavailable" ? (
                  "Unavailable"
                ) : selectedPace?.observed_daily_minor_units == null ? (
                  "Warming up"
                ) : (
                  <>{m(selectedPace.observed_daily_minor_units)} / day</>
                )}
              </Text>
            </Group>
            <Text size="sm" c="dimmed">
              Provisional leash ~{m(allowance.daily_reference_minor_units)} / day.
            </Text>
            {(estimateState === "loading" || (unmatched && unmatched.count > 0)) && (
              <Text size="xs" c="dimmed">
                {estimateState === "loading" ? (
                  <>
                    Unmatched in selected window:{" "}
                    <Skeleton component="span" height={10} width={90} display="inline-block" />
                  </>
                ) : (
                  <>
                    Unmatched in selected window: {unmatched?.count ?? 0} ({m(unmatched?.amount_minor_units)}) · counted
                    as flexible.
                  </>
                )}
              </Text>
            )}
            {(estimateState === "loading" || (currentEstimate && allowance.forecast.estimated_exhaustion_at)) && (
              <Text size="sm">
                Without future credits, this pace would use up the cushion around{" "}
                {estimateState === "loading" ? (
                  <Skeleton component="span" height={12} width={130} display="inline-block" />
                ) : (
                  <strong>{time(allowance.forecast.estimated_exhaustion_at)}</strong>
                )}
                .
              </Text>
            )}
          </Stack>
        </Paper>
      </SimpleGrid>
      <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="lg">
        <PurchaseCheck
          allowance={allowance}
          purchase={purchase}
          onPurchaseChange={onPurchaseChange}
          estimateState={estimateState}
          estimatePeriodId={estimatePeriodId}
        />
        <Card component="section" aria-labelledby="cycle-title" withBorder radius="lg" padding="xl">
          <Stack gap="lg">
            <Group gap="xs">
              <Title id="cycle-title" order={2} size="h3">
                Where you stand
              </Title>
              <HelpTooltip
                topic="Carryforward"
                description="Unused allowance carries forward. Each monthly credit adds to the balance instead of resetting it at month-end."
              />
            </Group>
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
                    - {m(cycleSpend?.spend_minor_units)}
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
                Allowance began: {allowance.activation_at} at midnight ({allowance.time_zone}).
              </Text>
              <Text size="sm" c="dimmed">
                These are overlapping views of the same purchases, not separate budgets. Your available balance includes
                all credits since activation, less posted and pending flexible spending.
              </Text>
              <SimpleGrid cols={{ base: 2, sm: 4 }} spacing="lg">
                {historyPeriods.map(({ id, label }) => {
                  const report = spendPeriod(allowance, id);
                  return (
                    <Metric
                      key={id}
                      label={label}
                      value={m(report?.spend_minor_units)}
                      detail={report ? `Counted from ${report.counted_from}` : undefined}
                    />
                  );
                })}
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
        : card.statement_period.kind === "provisional"
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
          {card.statement_period.kind === "statement" ? (
            <>
              This statement
              {card.limit_minor_units != null && (
                <>
                  {" "}
                  · <Money value={card.limit_minor_units} currency={card.currency} /> card limit
                </>
              )}
            </>
          ) : card.statement_period.kind === "provisional" ? (
            `Provisional card total since ${card.statement_period.start}; statement date not yet reported. Includes purchases outside the allowance.`
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

function SpendingSkeleton() {
  return (
    <Stack gap="xl" role="status" aria-label="Loading spending view" aria-busy="true">
      <Text size="sm" c="dimmed">
        Loading your spending data…
      </Text>
      <Card withBorder radius="lg" padding="xl" aria-hidden="true">
        <Stack gap="md">
          <Group justify="space-between" gap="md">
            <Skeleton height={22} width="28%" />
            <Skeleton height={24} width={130} radius="xl" />
          </Group>
          <Skeleton height={250} radius="md" />
          <SimpleGrid cols={{ base: 1, sm: 2, md: 3 }} spacing="sm">
            {[0, 1, 2].map((index) => (
              <Skeleton key={index} height={22} />
            ))}
          </SimpleGrid>
        </Stack>
      </Card>
      <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="lg" aria-hidden="true">
        {[0, 1].map((index) => (
          <Paper key={index} withBorder radius="lg" p="xl">
            <Stack gap="md">
              <Group justify="space-between">
                <Skeleton height={16} width="52%" />
                <Skeleton height={20} width={112} radius="xl" />
              </Group>
              <Skeleton height={46} width="58%" />
              <Skeleton height={14} width="76%" />
              <Skeleton height={1} />
              <Skeleton height={16} width="64%" />
              <Skeleton height={34} width="48%" />
            </Stack>
          </Paper>
        ))}
      </SimpleGrid>
      <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="lg" aria-hidden="true">
        {[0, 1].map((index) => (
          <Card key={index} withBorder radius="lg" padding="xl">
            <Stack gap="md">
              <Skeleton height={22} width="40%" />
              <Skeleton height={40} />
              <Skeleton height={16} width="75%" />
              <Skeleton height={68} radius="md" />
            </Stack>
          </Card>
        ))}
      </SimpleGrid>
      <section>
        <Group justify="space-between" align="baseline" mb="md">
          <Title order={2} size="h3">
            Card statements
          </Title>
          <Skeleton height={16} width={72} aria-hidden="true" />
        </Group>
        <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="md" aria-hidden="true">
          {[0, 1].map((index) => (
            <Card key={index} withBorder radius="md" padding="lg">
              <Stack gap="sm">
                <Skeleton height={20} width="54%" />
                <Skeleton height={34} width="38%" />
                <Skeleton height={16} width="80%" />
              </Stack>
            </Card>
          ))}
        </SimpleGrid>
      </section>
    </Stack>
  );
}

export function SpendingPage({
  view,
  connectionState,
  error,
  transactions,
  transactionsLoading,
  transactionsError,
  estimatePeriodId,
  estimatePending,
  purchase,
  onPurchaseChange,
  onEstimatePeriodChange,
}: {
  view: SpendView | null;
  connectionState: string;
  error: string | null;
  transactions: SpendTransactionsView | null;
  transactionsLoading: boolean;
  transactionsError: string | null;
  estimatePeriodId: EstimatePeriodId;
  estimatePending: boolean;
  purchase: number | string;
  onPurchaseChange: (value: number | string) => void;
  onEstimatePeriodChange: (periodId: EstimatePeriodId) => void;
}) {
  const cards = view?.cards || [];
  const estimateState: EstimateState = !estimatePending
    ? "current"
    : error || connectionState === "Unable to connect"
      ? "unavailable"
      : "loading";
  const matchingTransactions = transactions?.requested_period_id === estimatePeriodId ? transactions : null;
  return (
    <Stack gap="xl">
      <Group justify="space-between" gap="md">
        <Title order={1} size="h3">
          Flexible spending
        </Title>
        <Badge color={connectionState === "Live updates" ? "teal" : "gray"} variant="dot" aria-live="polite">
          {connectionState}
        </Badge>
      </Group>
      {view?.allowance?.status === "active" && (
        <SpendingHistoryChart
          transactions={matchingTransactions}
          allowance={view.allowance}
          loading={transactionsLoading || matchingTransactions == null}
          error={transactionsError}
          periodId={estimatePeriodId}
          onPeriodChange={onEstimatePeriodChange}
        />
      )}
      {error && (
        <Alert color="red" title={view ? "Couldn't refresh your view" : "Couldn't load your spending view"}>
          {view
            ? `${error}. Showing the last loaded view from ${time(view.generated_at)}.`
            : `${error}. No spending view has loaded yet.`}
        </Alert>
      )}
      {!error && connectionState === "Unable to connect" && view && (
        <Alert color="yellow" title="Live updates disconnected">
          The latest view was fetched at {time(view.generated_at)}, but live updates are unavailable.
        </Alert>
      )}
      {!view && !error ? <SpendingSkeleton /> : null}
      {view?.allowance ? (
        <>
          {view.allowance.status === "active" && view.allowance.note && (
            <Alert color="yellow" title="Allowance needs review" style={{ overflowWrap: "anywhere" }}>
              {view.allowance.note}
            </Alert>
          )}
          <AllowancePanel
            allowance={view.allowance}
            purchase={purchase}
            onPurchaseChange={onPurchaseChange}
            estimateState={estimateState}
            estimatePeriodId={estimatePeriodId}
          />
        </>
      ) : (
        view && (
          <Alert color="yellow" title="No flexible allowance yet">
            No allowance is configured. Card totals below are not a flexible-spend budget.
          </Alert>
        )
      )}
      {view && (
        <section aria-labelledby="cards-title">
          <Group justify="space-between" align="baseline">
            <Title order={2} id="cards-title" size="h3">
              Card statements
            </Title>
            <Text size="sm" c="dimmed">
              {cards.length} {cards.length === 1 ? "card" : "cards"}
            </Text>
          </Group>
          <Group gap="xs" mb="md">
            <Text size="xs" c="dimmed">
              Separate from the flexible allowance
            </Text>
            <HelpTooltip
              topic="Card statements"
              description="Card totals follow statement cycles and include purchases outside the flexible allowance. Card limits are separate from the flexible spending budget."
            />
          </Group>
          <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="md">
            {cards.map((card, index) => (
              <SpendCard card={card} key={`${card.account_name}-${index}`} />
            ))}
          </SimpleGrid>
          {!cards.length && (
            <Text size="sm" c="dimmed">
              No cards configured.
            </Text>
          )}
        </section>
      )}
      {view && (
        <>
          <Divider />
          <Group justify="space-between" gap="sm">
            <Group gap="xs">
              <Text size="xs" c="dimmed">
                Advisory only
              </Text>
              <HelpTooltip
                topic="Advisory only"
                description="This dashboard does not block bank transactions. Review your accounts for decisions that matter; account sync and transaction posting may lag."
              />
            </Group>
            <Text size="xs" c="dimmed">
              View updated {time(view.generated_at)}
            </Text>
          </Group>
        </>
      )}
    </Stack>
  );
}
