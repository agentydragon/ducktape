import { Code, Group, Stack, Text } from "@mantine/core";
import type { JSX, ReactNode } from "react";

import { serviceAccountKey } from "../connections/models";
import { Disclosure, DisclosureBody } from "../disclosure";
import { TimeAgo } from "../time_ago";
import { ClampedBlock, lineCount, TEXT_MAX_HEIGHT_REM } from "../clamped_block";
import { JsonBlock } from "../text_block";
import { RawSwitch } from "../raw_switch";
import type { ActionRequestView } from "./client";
import {
  renderActionCall,
  renderActionLabel,
  renderDetailsArguments,
  type RenderedActionCall,
} from "./rendering/index";

/** Who asked and when, on one summary line ("claude-ai · 2 minutes ago"), over the request's own
 * verbose identifiers nobody reads at a glance: the UUID, the caller's namespace, and the later
 * timestamps. */
function RequestAuditDetails({ request }: { request: ActionRequestView }): JSX.Element {
  return (
    <Disclosure
      summary={
        <Text component="span" size="xs" c="dimmed">
          {request.caller.name} · <TimeAgo value={request.created_at} />
        </Text>
      }
      keepMounted
    >
      <DisclosureBody>
        <Stack gap={2}>
          <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
            Request <Code>{request.id}</Code>
          </Text>
          <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
            Requested by {serviceAccountKey(request.caller)}
          </Text>
          {request.decision && (
            <Text size="xs" c="dimmed">
              Decided <TimeAgo value={request.decision.decided_at} />
            </Text>
          )}
          {request.execution?.started_at && (
            <Text size="xs" c="dimmed">
              Started <TimeAgo value={request.execution.started_at} />
            </Text>
          )}
          {request.execution?.completed_at && (
            <Text size="xs" c="dimmed">
              Completed <TimeAgo value={request.execution.completed_at} />
            </Text>
          )}
        </Stack>
      </DisclosureBody>
    </Disclosure>
  );
}

/** Everything describing the call itself -- which Action, who asked for it and in what words, and
 * its exact arguments -- drawn the same on the pending card, where the operator decides it, and on
 * the history card, where it is audited. What the card adds about the decision or the outcome goes
 * in `status` or `headerActions` beside the Action's identity, or after this.
 *
 * The card's one Raw switch sits beside its status or decision controls, at the top of the card so
 * it stays under the pointer while switching changes the height of what follows. It governs the
 * whole card, and shows only when something on the card renders other than as its stored JSON. */
export function ActionCall({
  request,
  status,
  headerActions,
  raw,
  onRawChange,
  prettyResult,
  combinedCall: providedCombinedCall,
  detailsOnly = false,
}: {
  request: ActionRequestView;
  /** The history summary already presents identity, intent, status and Raw controls. */
  detailsOnly?: boolean;
  status?: ReactNode;
  headerActions?: ReactNode;
  raw: boolean;
  onRawChange: (raw: boolean) => void;
  /** Whether the card's result renders other than as its stored JSON. */
  prettyResult: boolean;
  /** Precomputed by history so it can hide a result already shown in this view. */
  combinedCall?: RenderedActionCall | null;
}): JSX.Element {
  const actionLabel = renderActionLabel(request.action, request.arguments);
  const prettyArguments = renderDetailsArguments(request.action, request.arguments);
  const combinedCall =
    providedCombinedCall === undefined ? renderActionCall(request.action, request.arguments) : providedCombinedCall;
  const showCombinedCall = combinedCall !== undefined && combinedCall !== null && combinedCall.content != null;
  return (
    <Stack className="agentplane-action-operation-host" data-operation-density="detail" gap="sm">
      <Stack gap={2}>
        {!detailsOnly && (
          <Group justify="space-between" align="flex-start" gap="xs">
            {actionLabel === undefined ? (
              <Text fw={600} ff="monospace" style={{ overflowWrap: "anywhere" }}>
                {request.action.group} / {request.action.name}
              </Text>
            ) : (
              actionLabel
            )}
            <Group gap="xs" justify="flex-end" wrap="wrap">
              {status}
              {(prettyArguments !== undefined ||
                prettyResult ||
                (combinedCall !== undefined && combinedCall !== null)) && (
                <RawSwitch raw={raw} onChange={onRawChange} />
              )}
              {headerActions}
            </Group>
          </Group>
        )}
        {/* TODO: the caller's own framing renders verbatim as plain text; markdown rendering is a
            possible later addition. */}
        {!detailsOnly && <Text size="sm">{request.title}</Text>}
        {request.description && (
          <ClampedBlock maxHeightRem={TEXT_MAX_HEIGHT_REM} lines={lineCount(request.description)} label="Description">
            <Text size="xs" c="dimmed">
              {request.description}
            </Text>
          </ClampedBlock>
        )}
        <RequestAuditDetails request={request} />
      </Stack>
      {showCombinedCall && !raw ? (
        <div>
          <Text size="sm" fw={600} mb={4}>
            Call
          </Text>
          {combinedCall.content}
        </div>
      ) : combinedCall?.includesResult && combinedCall.content != null && !raw ? null : (
        <div>
          {!(prettyArguments === null && !raw) && (
            <>
              <Text size="sm" fw={600} mb={4}>
                Exact arguments
              </Text>
              {prettyArguments === undefined || raw ? <JsonBlock value={request.arguments} /> : prettyArguments}
            </>
          )}
        </div>
      )}
    </Stack>
  );
}
