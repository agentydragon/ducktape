import { Code, Group, Stack, Text } from "@mantine/core";
import type { JSX, ReactNode } from "react";

import { renderArguments } from "./action_rendering/index";
import { serviceAccountKey, type ActionRequestView } from "./client";
import { JsonView } from "./json_view";
import { RawSwitch } from "./raw_switch";

/** The grant fields, folded inside `RequestAuditDetails`' disclosure rather than shown
 * unconditionally: verbose per-request provenance an operator deciding needs occasionally, not on
 * every glance at the card. */
function ExternalGrantFields({ grant }: { grant: NonNullable<ActionRequestView["external_grant"]> }): JSX.Element {
  return (
    <Stack gap={2} mt={4} style={{ overflowWrap: "anywhere" }}>
      <Text size="xs">
        Acts as <Code>{serviceAccountKey(grant.caller)}</Code> · Client <Code>{grant.client_id}</Code>
      </Text>
      <Text size="xs">
        Issuer <Code>{grant.issuer}</Code>
      </Text>
      <Text size="xs">
        Connection <Code>{grant.connection_id}</Code>
      </Text>
      <Text size="xs">
        Grant <Code>{grant.grant_id}</Code> · Revision <Code>{grant.revision}</Code>
      </Text>
      <Text size="xs" c="dimmed">
        Historical submission evidence, not the connection’s current authorization status.
      </Text>
    </Stack>
  );
}

/** The request's own verbose identifiers, folded behind one disclosure rather than shown
 * unconditionally: the UUID nobody reads at a glance, and -- when the caller authenticated
 * externally -- the grant provenance behind it. */
function RequestAuditDetails({ request }: { request: ActionRequestView }): JSX.Element {
  const grant = request.external_grant;
  return (
    <details>
      <Text component="summary" size="xs" c="dimmed" style={{ cursor: "pointer" }}>
        {grant ? "Request & grant audit details" : "Request audit details"}
      </Text>
      <Stack gap={2} mt={4}>
        <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
          Request <Code>{request.id}</Code>
        </Text>
        {grant && <ExternalGrantFields grant={grant} />}
      </Stack>
    </details>
  );
}

/** Everything describing the call itself -- which Action, who asked for it and in what words, and
 * its exact arguments -- drawn the same on the pending card, where the operator decides it, and on
 * the history card, where it is audited. What the card adds about the decision or the outcome goes
 * in `status`, beside the Action's identity, or after this.
 *
 * The card's one Raw switch sits beside `status`, at the top of the card so it stays under the
 * pointer while switching changes the height of what follows. It governs the whole card, and shows
 * only when something on the card renders other than as its stored JSON. */
export function ActionCall({
  request,
  status,
  raw,
  onRawChange,
  prettyResult,
}: {
  request: ActionRequestView;
  status: ReactNode;
  raw: boolean;
  onRawChange: (raw: boolean) => void;
  /** Whether the card's result renders other than as its stored JSON. */
  prettyResult: boolean;
}): JSX.Element {
  const prettyArguments = renderArguments(request.action, request.arguments);
  return (
    <Stack gap="sm">
      <Stack gap={2}>
        <Group justify="space-between" align="flex-start" gap="xs">
          <Text fw={600} ff="monospace" style={{ overflowWrap: "anywhere" }}>
            {request.action.group} / {request.action.name}
          </Text>
          <Group gap="xs">
            {status}
            {(prettyArguments !== null || prettyResult) && <RawSwitch raw={raw} onChange={onRawChange} />}
          </Group>
        </Group>
        {/* TODO: the caller's own framing renders verbatim as plain text; markdown rendering is a
            possible later addition. */}
        <Text size="sm">{request.title}</Text>
        {request.description && (
          <Text size="xs" c="dimmed">
            {request.description}
          </Text>
        )}
        {request.caller && (
          <Text size="xs" c="dimmed">
            requested by {serviceAccountKey(request.caller)}
          </Text>
        )}
        {/* The grant provenance behind an external caller is in the audit details below. */}
        {request.external_grant && (
          <Text size="xs" fw={600}>
            Authenticated external caller at submission
          </Text>
        )}
        <RequestAuditDetails request={request} />
      </Stack>
      <div>
        <Text size="sm" fw={600} mb={4}>
          Exact arguments (unredacted)
        </Text>
        {prettyArguments === null || raw ? <JsonView value={request.arguments} /> : prettyArguments}
      </div>
    </Stack>
  );
}
