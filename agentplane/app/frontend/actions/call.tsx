import { Box, Code, Group, Stack, Text } from "@mantine/core";
import type { JSX, ReactNode } from "react";

import { serviceAccountKey } from "../connections/service_accounts";
import { Disclosure, DisclosureBody } from "../../../../util/frontend/disclosure";
import { TimeAgo } from "../ui/time_ago";
import { ClampedBlock, lineCount, TEXT_MAX_HEIGHT_REM } from "../../../../util/frontend/clamped_block";
import { JsonBlock } from "../ui/text_block";
import { RawSwitch } from "../ui/raw_switch";
import { ActionState, type ActionRequest } from "../../../action_service/api/contracts/action_service_pb";
import { actionArguments, actionCaller, actionIdentity, timestampText, type ActionCaller } from "./presentation";
import { useActionRendering, type RenderedActionCall } from "./rendering/dispatch";

/** Who asked and when, on one summary line ("claude-ai · 2 minutes ago"), over the request's own
 * verbose identifiers nobody reads at a glance: the UUID, the caller's namespace, and the later
 * timestamps. */
function RequestAuditDetails({ request }: { request: ActionRequest }): JSX.Element {
  const caller = actionCaller(request);
  const callerSummary = actionCallerSummary(caller);
  const callerDetails = actionCallerDetails(caller);
  const createdAt = timestampText(request.createdAt);
  const decidedAt = timestampText(request.decision?.decidedAt);
  const startedAt = timestampText(request.execution?.startedAt);
  const completedAt = timestampText(request.execution?.completedAt);
  return (
    <Disclosure
      summary={
        <Text component="span" size="xs" c="dimmed">
          {callerSummary} · <TimeAgo value={createdAt ?? ""} />
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
            Requested by {callerDetails}
          </Text>
          {decidedAt && (
            <Text size="xs" c="dimmed">
              Decided <TimeAgo value={decidedAt} />
            </Text>
          )}
          {startedAt && (
            <Text size="xs" c="dimmed">
              Started <TimeAgo value={startedAt} />
            </Text>
          )}
          {completedAt && (
            <Text size="xs" c="dimmed">
              Completed <TimeAgo value={completedAt} />
            </Text>
          )}
        </Stack>
      </DisclosureBody>
    </Disclosure>
  );
}

function actionCallerSummary(caller: ActionCaller): string {
  switch (caller.kind) {
    case "service-account":
      return caller.account.name;
    case "identity":
      return `Identity ${caller.identity.name}`;
    case "unknown":
      return "Unknown caller";
  }
}

function actionCallerDetails(caller: ActionCaller): string {
  switch (caller.kind) {
    case "service-account":
      return caller.uid === undefined
        ? serviceAccountKey(caller.account)
        : `${serviceAccountKey(caller.account)} · UID ${caller.uid}`;
    case "identity":
      return `Identity ${caller.identity.name} · UID ${caller.identity.uid}`;
    case "unknown":
      return "an unknown caller";
  }
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
  request: ActionRequest;
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
  const rendering = useActionRendering();
  const action = actionIdentity(request);
  const args = actionArguments(request);
  const actionLabel = rendering.renderActionLabel(action, args, request.id);
  const prettyArguments = rendering.renderDetailsArguments(
    action,
    args,
    request.id,
    request.state === ActionState.DECISION_PENDING
  );
  const combinedCall =
    providedCombinedCall === undefined
      ? rendering.renderActionCall(action, args, undefined, request.id)
      : providedCombinedCall;
  const callContent =
    !raw && combinedCall?.content != null ? (
      combinedCall.content
    ) : raw || prettyArguments === undefined ? (
      <JsonBlock heading="Exact arguments" value={args} />
    ) : (
      prettyArguments
    );
  return (
    <Stack className="agentplane-action-operation-host" data-operation-density="detail" gap="xs">
      <Stack gap={2}>
        {!detailsOnly && (
          <Group justify="space-between" align="flex-start" gap="xs">
            {actionLabel === undefined ? (
              <Text fw={600} ff="monospace" style={{ overflowWrap: "anywhere" }}>
                {action.group} / {action.name}
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
        {!detailsOnly && rendering.shouldRenderRequestTitle(action, args, request.title, actionLabel) && (
          <Text size="sm" style={{ overflowWrap: "anywhere" }}>
            {request.title}
          </Text>
        )}
        {request.description?.trim() && (
          <Box style={{ "--ui-disclosure-gutter": "0px", "--ui-disclosure-body-spacing": "0px" }}>
            <ClampedBlock maxHeightRem={TEXT_MAX_HEIGHT_REM} lines={lineCount(request.description)} label="Description">
              <Text size="sm" c="dimmed" style={{ overflowWrap: "anywhere", whiteSpace: "pre-wrap" }}>
                {request.description}
              </Text>
            </ClampedBlock>
          </Box>
        )}
        <RequestAuditDetails request={request} />
      </Stack>
      {callContent}
    </Stack>
  );
}
