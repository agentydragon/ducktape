import { fromJson, type JsonValue } from "@bufbuild/protobuf";
import { Alert, Box, Stack, Table, Text, Title } from "@mantine/core";
import { type JSX, useEffect, useState } from "react";

import {
  GetActionPolicyResponseSchema,
  type CallerActionPolicy,
  type EffectivePolicy,
} from "../../../action_service/action_service_pb";
import { CodeBlock } from "../../../../util/frontend/code_block";
import { followWatch } from "../watch_stream";

/** The Action Service's own auto-approval policy, in evaluation order. */
export function ActionPolicySection({
  policy,
  error = null,
}: {
  policy: CallerActionPolicy | null;
  error?: string | null;
}): JSX.Element {
  if (error)
    return (
      <Alert color="red" role="alert" title="The Action Service could not be asked">
        {error}
      </Alert>
    );
  if (!policy) return <></>;
  return (
    <Stack gap="md">
      {!policy.synced && (
        <Alert color="orange" role="alert" title="The Action Service's watch has not synced">
          Nothing auto-decides until it has: every Action from this ServiceAccount waits for the operator, whatever the
          objects say.
        </Alert>
      )}
      <Title order={5}>Auto-approve if</Title>
      {policy.autoApproveIf.length === 0 ? (
        <Text size="sm" c="dimmed">
          Nothing: every Action from this ServiceAccount waits for the operator.
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
            {policy.autoApproveIf.map((effective, position) => (
              <PolicyRow
                key={`${effective.binding}/${effective.policySet}/${effective.source}/${effective.index}`}
                effective={effective}
                position={position}
              />
            ))}
          </Table.Tbody>
        </Table>
      )}
    </Stack>
  );
}

function PolicyRow({ effective, position }: { effective: EffectivePolicy; position: number }): JSX.Element {
  const kind = effective.policy?.kind;
  const actions = kind?.value?.actions ?? [];
  const schema = kind?.case === "argumentSchema" ? kind.value.argumentSchema : undefined;
  const kindName = kind?.case?.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`) ?? "unknown";
  return (
    <Table.Tr>
      <Table.Td>
        <Text size="sm">{position + 1}</Text>
      </Table.Td>
      <Table.Td>
        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
          {effective.binding} · {effective.policySet}[{effective.index}]
        </Text>
        <Text size="xs" c="dimmed">
          {kindName}
        </Text>
      </Table.Td>
      <Table.Td>
        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
          {actions.map(({ group, names }) => `${group}: ${names.join(", ")}`).join(" · ")}
        </Text>
        {schema !== undefined && (
          <Box hiddenFrom="sm" mt="xs">
            <CodeBlock text={JSON.stringify(schema)} language="json" />
          </Box>
        )}
      </Table.Td>
      <Table.Td visibleFrom="sm">
        {schema !== undefined ? (
          <CodeBlock text={JSON.stringify(schema, null, 1)} language="json" />
        ) : (
          <Text size="sm" c="dimmed">
            any
          </Text>
        )}
      </Table.Td>
    </Table.Tr>
  );
}

/** Watch the subject directly through Action Service's generated HTTP facade. */
export function ActionPolicyWatch({
  subject,
  fence = "",
}: {
  subject: { namespace: string; name: string } | null | undefined;
  fence?: string;
}): JSX.Element {
  const namespace = subject?.namespace ?? "";
  const name = subject?.name ?? "";
  const identity = namespace && name ? `${fence}\0${namespace}\0${name}` : "";
  const [observed, setObserved] = useState<{ identity: string; policy: CallerActionPolicy } | null>(null);
  const [failure, setFailure] = useState<{ identity: string; error: string } | null>(null);
  useEffect(() => {
    if (!identity) return;
    let active = true;
    const params = new URLSearchParams({ "subject.namespace": namespace, "subject.name": name });
    const stop = followWatch(`/upstream/actions/v1/action-policy:watch?${params}`, {
      onOpen: () => {
        if (active) {
          setObserved(null);
          setFailure(null);
        }
      },
      onFrame: (result) => {
        const policy = fromJson(GetActionPolicyResponseSchema, result as JsonValue).actionPolicy;
        if (!policy || policy.subject?.namespace !== namespace || policy.subject.name !== name)
          throw new Error("Action Service returned a different subject");
        if (active) setObserved({ identity, policy });
      },
      onConnection: (connection) => {
        if (!active) return;
        if (connection.phase === "reconnecting") {
          setObserved(null);
          setFailure({ identity, error: connection.lastError ?? "Action policy watch disconnected" });
        }
      },
    });
    return () => {
      active = false;
      stop();
    };
  }, [namespace, name, identity]);
  return (
    <ActionPolicySection
      policy={observed?.identity === identity ? observed.policy : null}
      error={failure?.identity === identity ? failure.error : null}
    />
  );
}
