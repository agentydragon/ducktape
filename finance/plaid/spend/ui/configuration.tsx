import {
  Alert,
  Badge,
  Box,
  Card,
  Divider,
  Group,
  Paper,
  SimpleGrid,
  Skeleton,
  Stack,
  Text,
  Title,
} from "@mantine/core";

import type { SpendConfigurationView } from "./schema";
import { Metric, Money, periodLabels } from "./spend_display";
import { ruleConditionText, ruleKindDisplay, rulesFootnote } from "./rule_display";
import { HelpTooltip } from "../../../../util/frontend/help_tooltip";

function ConfigurationSkeleton() {
  return (
    <Stack gap="lg" role="status" aria-label="Loading configuration" aria-busy="true">
      <Text size="sm" c="dimmed">
        Loading configuration…
      </Text>
      <SimpleGrid cols={{ base: 1, sm: 2, md: 5 }} spacing="md" aria-hidden="true">
        {Array.from({ length: 5 }, (_, index) => (
          <Stack key={index} gap="xs">
            <Skeleton height={12} width="68%" />
            <Skeleton height={28} width="82%" />
          </Stack>
        ))}
      </SimpleGrid>
      <Card withBorder radius="lg" p="lg" aria-hidden="true">
        <Stack gap="md">
          <Skeleton height={22} width="42%" />
          <SimpleGrid cols={{ base: 1, sm: 2, md: 5 }} spacing="md">
            {Array.from({ length: 5 }, (_, index) => (
              <Stack key={index} gap="xs">
                <Skeleton height={12} width="72%" />
                <Skeleton height={24} width="60%" />
              </Stack>
            ))}
          </SimpleGrid>
          <Skeleton height={1} />
          <Skeleton height={18} width="28%" />
          <SimpleGrid cols={{ base: 1, sm: 2, md: 3 }} spacing="xs">
            {Array.from({ length: 6 }, (_, index) => (
              <Skeleton key={index} height={20} />
            ))}
          </SimpleGrid>
          <Skeleton height={1} />
          <Skeleton height={18} width="34%" />
          {Array.from({ length: 3 }, (_, index) => (
            <Skeleton key={index} height={58} radius="md" />
          ))}
        </Stack>
      </Card>
      <section aria-hidden="true">
        <Skeleton height={24} width="24%" mb="sm" />
        <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="md">
          {[0, 1].map((index) => (
            <Card key={index} withBorder radius="md" p="lg">
              <Stack gap="sm">
                <Skeleton height={20} width="62%" />
                <Skeleton height={16} width="78%" />
                <Skeleton height={16} width="54%" />
              </Stack>
            </Card>
          ))}
        </SimpleGrid>
      </section>
    </Stack>
  );
}

export function ConfigurationPage({
  configuration,
  error,
}: {
  configuration: SpendConfigurationView | null;
  error: string | null;
}) {
  const allowance = configuration?.allowance;
  const reversalPairs = allowance?.reversal_pairs ?? [];
  return (
    <Stack gap="lg">
      <Group justify="space-between" align="flex-start" gap="md">
        <div>
          <Group gap="xs" align="center">
            <Title order={2} size="h3">
              Configuration
            </Title>
            <HelpTooltip
              topic="Configuration"
              description="These read-only settings are loaded by Plaid Spend. Account IDs are omitted. Changes are made in the private configuration and take effect after rollout."
            />
          </Group>
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
      {!configuration && !error && <ConfigurationSkeleton />}
      {configuration && (
        <>
          <Metric label="DAY CUTOVER TIME ZONE" value={configuration.time_zone} />
          {allowance ? (
            <Card component="section" aria-labelledby="configuration-allowance-title" withBorder radius="lg" p="lg">
              <Stack gap="md">
                <Title id="configuration-allowance-title" order={3} size="h4">
                  Flexible allowance policy
                </Title>
                <SimpleGrid cols={{ base: 1, sm: 2, md: 5 }} spacing="md">
                  <Metric
                    label="MONTHLY ALLOWANCE"
                    value={<Money value={allowance.monthly_minor_units} currency={allowance.currency} />}
                  />
                  <Metric label="START DATE" value={allowance.activation_at} />
                  <Metric label="ACCOUNTS IN SCOPE" value={allowance.spending_account_count} />
                  <Metric label="MAX SYNC AGE" value={`${allowance.max_sync_age_hours} hours`} />
                  <Metric label="DEFAULT ESTIMATE WINDOW" value={periodLabels[allowance.forecast_basis_period_id]} />
                </SimpleGrid>
                <Divider />
                <div>
                  <Text fw={650} mb="sm">
                    Analysis categories
                  </Text>
                  <SimpleGrid cols={{ base: 1, sm: 2, md: 3 }} spacing="xs">
                    {Object.entries(allowance.analysis_categories).map(([id, category]) => (
                      <Group key={id} gap="xs" wrap="nowrap">
                        <Box
                          component="span"
                          aria-hidden="true"
                          style={{
                            display: "inline-block",
                            width: 10,
                            height: 10,
                            flex: "0 0 auto",
                            borderRadius: "50%",
                            backgroundColor: category.color,
                          }}
                        />
                        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
                          {category.label}
                        </Text>
                        <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                          {id}
                        </Text>
                      </Group>
                    ))}
                  </SimpleGrid>
                </div>
                <Divider />
                <div>
                  <Group gap="xs" mb="sm">
                    <Text fw={650}>Classification rules</Text>
                    <HelpTooltip topic="How rules work" description={rulesFootnote} />
                  </Group>
                  <Stack gap="xs">
                    {allowance.rules.map((rule, index) => {
                      const { label, color } = ruleKindDisplay[rule.kind];
                      const oneOff = rule.scope === "one_off";
                      return (
                        <Paper key={`${rule.kind}-${index}`} withBorder radius="md" p="sm">
                          <Group align="flex-start" gap="sm" wrap="nowrap">
                            <Text size="sm" c="dimmed" w={20} ta="right">
                              {index + 1}.
                            </Text>
                            <Badge color={color} variant="light" style={{ flexShrink: 0 }}>
                              {label}
                            </Badge>
                            {oneOff && (
                              <Badge color="violet" variant="light" style={{ flexShrink: 0 }}>
                                One-off
                              </Badge>
                            )}
                            <Stack gap={2} miw={0}>
                              {rule.id && (
                                <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
                                  {rule.id}
                                </Text>
                              )}
                              <Text size="sm" style={{ overflowWrap: "anywhere" }}>
                                {ruleConditionText(rule.condition)}
                              </Text>
                              {rule.description && (
                                <details>
                                  <summary style={{ cursor: "pointer" }}>
                                    <Text span size="xs" c="dimmed">
                                      Rule explanation
                                    </Text>
                                  </summary>
                                  <Text size="xs" c="dimmed" mt={4} style={{ overflowWrap: "anywhere" }}>
                                    {rule.description}
                                  </Text>
                                </details>
                              )}
                            </Stack>
                          </Group>
                        </Paper>
                      );
                    })}
                  </Stack>
                </div>
                <Divider />
                <div>
                  <Group gap="xs" mb="sm">
                    <Text fw={650}>Full reversal pairs</Text>
                    <HelpTooltip
                      topic="Full reversals"
                      description="A pair excludes both legs only when each selector finds one posted transaction with the exact opposite amount in the allowance currency and the same account. Unresolved or overlapping matches keep every transaction in normal allowance accounting."
                    />
                  </Group>
                  {reversalPairs.length ? (
                    <Stack gap="xs">
                      {reversalPairs.map((pair) => (
                        <Paper key={pair.id} withBorder radius="md" p="sm">
                          <Stack gap="xs">
                            <Text size="sm" fw={650} style={{ overflowWrap: "anywhere" }}>
                              {pair.id}
                            </Text>
                            <Text size="sm" c="dimmed">
                              {pair.description}
                            </Text>
                            <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="xs">
                              <div>
                                <Badge color="blue" variant="light" mb={4}>
                                  Original debit
                                </Badge>
                                <Text size="sm" style={{ overflowWrap: "anywhere" }}>
                                  {ruleConditionText(pair.original)}
                                </Text>
                              </div>
                              <div>
                                <Badge color="teal" variant="light" mb={4}>
                                  Reversal credit
                                </Badge>
                                <Text size="sm" style={{ overflowWrap: "anywhere" }}>
                                  {ruleConditionText(pair.reversal)}
                                </Text>
                              </div>
                            </SimpleGrid>
                          </Stack>
                        </Paper>
                      ))}
                    </Stack>
                  ) : (
                    <Text size="sm" c="dimmed">
                      No full reversal pairs configured.
                    </Text>
                  )}
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
              Limits are shown in minor units of each account&apos;s currency.
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
