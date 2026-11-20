import { type ActionPresentation, expandsForRejectedArguments } from "./presentation";
import { Code, Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import { definePreview, type ArgumentsPreview, type PreviewProps } from "./entry";

import { moveNodeArguments } from "../schemas/integration_previews";
import { LiveResourceLink, useIntegrationPreviewNames } from "./integration_previews";

const calendarNodeArguments = z.strictObject({
  workspaceId: z.string(),
  granularity: z.enum(["day", "week", "month", "year"]),
  date: z.string().optional(),
});

export function offersCardDecisionButtonsCalendarNode(args: unknown): boolean {
  return calendarNodeArguments.safeParse(args).success;
}

function CalendarNodeArguments({ args }: { args: z.infer<typeof calendarNodeArguments> }): JSX.Element {
  return (
    <Text size="sm" style={{ overflowWrap: "anywhere" }}>
      Get or create the <Code>{args.granularity}</Code> calendar node
      {args.date && (
        <>
          {" "}
          for <Code>{args.date}</Code>
        </>
      )}
      {" in workspace "}
      <Code>{args.workspaceId}</Code>.
    </Text>
  );
}

function CalendarNodeLabel({ args }: { args: z.infer<typeof calendarNodeArguments> }): JSX.Element {
  void args;
  return (
    <Text size="sm" fw={600}>
      Get or create Tana calendar node
    </Text>
  );
}

export const calendarNodeLabel: ArgumentsPreview = definePreview(calendarNodeArguments, CalendarNodeLabel);
export const calendarNodeArgumentsPreview: ArgumentsPreview = definePreview(
  calendarNodeArguments,
  CalendarNodeArguments
);

export const calendarNodePresentation: ActionPresentation = {
  label: calendarNodeLabel,
  pane: { collapsed: calendarNodeArgumentsPreview },
  details: { arguments: calendarNodeArgumentsPreview },
  offersCardDecisionButtons: offersCardDecisionButtonsCalendarNode,
  compactCardExpands: expandsForRejectedArguments(calendarNodeArguments),
};

const moveNode = definePreview(
  moveNodeArguments,
  function MoveNode({ args, requestId }: PreviewProps<z.infer<typeof moveNodeArguments>>): JSX.Element {
    const names = useIntegrationPreviewNames("tana", requestId);
    const node = (id: string) => (
      <LiveResourceLink id={id} name={names?.get(id)} href={`https://app.tana.inc?nodeid=${encodeURIComponent(id)}`} />
    );
    const atEdge = args.position === "start" || args.position === "end";
    const reference = args.referenceNodeId ? node(args.referenceNodeId) : null;
    return (
      <Text size="sm" style={{ overflowWrap: "anywhere" }}>
        Move {node(args.nodeId)}
        {args.sourceParentId && <> from {node(args.sourceParentId)}</>}
        {atEdge ? (
          ` to the ${args.position} of `
        ) : (
          <>
            {" "}
            {args.position} {reference ?? "(reference node missing)"} in{" "}
          </>
        )}
        {node(args.targetNodeId)}
        {atEdge && reference && <>, with reference {reference}</>}
        {args.keepSourceReference ? ", keeping the source reference." : ", removing the source reference."}
      </Text>
    );
  }
);

const moveNodePresentation: ActionPresentation = {
  fallbackLabel: "Move Tana node",
  pane: { opened: moveNode },
  details: { arguments: moveNode },
};

export const tanaRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  ["get_or_create_calendar_node", calendarNodePresentation],
  ["move_node", moveNodePresentation],
]);
