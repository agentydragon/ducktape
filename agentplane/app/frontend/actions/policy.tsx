import type { JSX } from "react";
import { Alert, Box, Code, Stack, Table, Text, Title } from "@mantine/core";

import type { ActionPolicyUnavailable, ActionPolicyView, EffectivePolicyView } from "../api/schema";
import { CodeBlock } from "../../../../util/frontend/code_block";

function actionsText(policy: EffectivePolicyView["policy"]): string {
  return Object.entries(policy.actions)
    .map(([group, names]) => `${group}: ${names.join(", ")}`)
    .join(" · ");
}

/** One policy list as the Action Service walks it: first match wins, in this order. */
function PolicyList({
  title,
  policies,
  empty,
}: {
  title: string;
  policies: EffectivePolicyView[];
  empty: string;
}): JSX.Element {
  return (
    <Stack gap="xs">
      <Title order={5}>{title}</Title>
      {policies.length === 0 ? (
        <Text size="sm" c="dimmed">
          {empty}
        </Text>
      ) : (
        <Table>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>#</Table.Th>
              <Table.Th>Via</Table.Th>
              <Table.Th>Actions</Table.Th>
              <Table.Th visibleFrom="sm">Arguments</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {policies.map((effective, position) => (
              <Table.Tr key={`${effective.binding}/${effective.policy_set}/${effective.source}/${effective.index}`}>
                <Table.Td>
                  <Text size="sm">{position + 1}</Text>
                </Table.Td>
                <Table.Td>
                  <Text size="sm" style={{ overflowWrap: "anywhere" }}>
                    {effective.binding} · {effective.policy_set}[{effective.index}]
                  </Text>
                  <Text size="xs" c="dimmed">
                    {effective.policy.type}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <Text size="sm" style={{ overflowWrap: "anywhere" }}>
                    {actionsText(effective.policy)}
                  </Text>
                  {/* On a phone the schema folds under the actions, wrapped so the table keeps
                      to the viewport. */}
                  {effective.policy.type === "argument_schema" && (
                    <Box hiddenFrom="sm" mt="xs">
                      <CodeBlock text={JSON.stringify(effective.policy.argument_schema)} language="json" />
                    </Box>
                  )}
                </Table.Td>
                <Table.Td visibleFrom="sm">
                  {effective.policy.type === "argument_schema" ? (
                    <CodeBlock text={JSON.stringify(effective.policy.argument_schema, null, 1)} language="json" />
                  ) : (
                    <Text size="sm" c="dimmed">
                      any
                    </Text>
                  )}
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
    </Stack>
  );
}

/** Why the frame carries no policy: the service could not be asked, and by whom or what. */
function Unavailable({ failure }: { failure: ActionPolicyUnavailable }): JSX.Element {
  return (
    <Alert color="red" role="alert" title="The Action Service could not be asked">
      <Text size="sm">
        <Code>{failure.code}</Code>
        {failure.upstream && (
          <>
            {" "}
            · {failure.upstream.method} {failure.upstream.url}:{" "}
            {failure.upstream.upstream_status ?? failure.upstream.error_type}
          </>
        )}
      </Text>
    </Alert>
  );
}

/**
 * The auto-approval list the Action Service walks for the ServiceAccount, as the service itself
 * resolves it. What arrives here is the service's answer, or why there is none. Read-only.
 */
export function ActionPolicySection({
  policy,
}: {
  policy: ActionPolicyView | ActionPolicyUnavailable | null;
}): JSX.Element {
  if (!policy) return <></>;
  if ("kind" in policy) return <Unavailable failure={policy} />;
  return (
    <Stack gap="md">
      {!policy.synced && (
        <Alert color="orange" role="alert" title="The Action Service's watch has not synced">
          Nothing auto-decides until it has: every Action from this ServiceAccount waits for the operator, whatever the
          objects say.
        </Alert>
      )}
      <PolicyList
        title="Auto-approve if"
        policies={policy.auto_approve_if}
        empty="Nothing: every Action from this ServiceAccount waits for the operator."
      />
    </Stack>
  );
}
