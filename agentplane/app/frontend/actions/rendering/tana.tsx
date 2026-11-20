import { type ActionPresentation, expandsForRejectedArguments } from "./presentation";
import { Code, Stack, Text } from "@mantine/core";
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
    const rows = [
      ["Node", args.nodeId],
      ["Destination", args.targetNodeId],
      ["Source parent", args.sourceParentId],
      ["Reference node", args.referenceNodeId],
    ] as const;
    return (
      <Stack gap={4}>
        {rows.map(([label, id]) =>
          id === undefined ? null : (
            <Text key={label} size="sm" style={{ overflowWrap: "anywhere" }}>
              {label}{" "}
              <LiveResourceLink
                id={id}
                name={names?.get(id)}
                href={`https://app.tana.inc?nodeid=${encodeURIComponent(id)}`}
              />
            </Text>
          )
        )}
        <Text size="sm">
          Position <Code>{args.position}</Code>
        </Text>
        <Text size="sm">{args.keepSourceReference ? "Keep the source reference" : "Remove the source reference"}</Text>
      </Stack>
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
