import { Code, Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import { moveNodeArguments } from "../schemas/integration_previews";
import { definePreview, type PreviewProps } from "./entry";
import { LiveResourceLink, useIntegrationPreviewNames } from "./integration_previews";
import { type ActionPresentation, expandsForRejectedArguments } from "./presentation";
import { defineResultPreview } from "./result_entry";

const calendarNodeArguments = z.strictObject({
  workspaceId: z.string(),
  granularity: z.enum(["day", "week", "month", "year"]),
  date: z.string().optional(),
});

function calendarPeriod(args: z.infer<typeof calendarNodeArguments>): string {
  if (args.date) {
    return args.granularity === "day" ? args.date : `${args.granularity} containing ${args.date}`;
  }
  return args.granularity === "day" ? "today" : `this ${args.granularity}`;
}

function TanaNodeLink({ id, name }: { id: string; name?: string }): JSX.Element {
  return <LiveResourceLink id={id} name={name} href={`https://app.tana.inc?nodeid=${encodeURIComponent(id)}`} />;
}

const calendarNode = definePreview(
  calendarNodeArguments,
  function CalendarNode({ args, requestId }: PreviewProps<z.infer<typeof calendarNodeArguments>>): JSX.Element {
    const workspace = useIntegrationPreviewNames("tana", requestId)?.get(args.workspaceId);
    return (
      <Text size="sm" style={{ overflowWrap: "anywhere" }}>
        For {calendarPeriod(args)} in {workspace ?? <Code>{args.workspaceId}</Code>}
      </Text>
    );
  }
);

const calendarResult = defineResultPreview(z.strictObject({ nodeId: z.string().min(1) }), ({ result, context }) => {
  const args = calendarNodeArguments.safeParse(context?.arguments);
  const label = args.success
    ? args.data.date
      ? `Calendar node for ${calendarPeriod(args.data)}`
      : `Open ${args.data.granularity} calendar node`
    : "Open calendar node";
  return (
    <Text size="sm">
      <TanaNodeLink id={result.nodeId} name={label} />
    </Text>
  );
});

const calendarNodePresentation: ActionPresentation = {
  fallbackLabel: "Get or create Tana calendar node",
  pane: { collapsed: calendarNode },
  details: { arguments: calendarNode, result: calendarResult },
  offersCardDecisionButtons: (args) => calendarNodeArguments.safeParse(args).success,
  compactCardExpands: expandsForRejectedArguments(calendarNodeArguments),
};

const moveNode = definePreview(
  moveNodeArguments,
  function MoveNode({ args, requestId }: PreviewProps<z.infer<typeof moveNodeArguments>>): JSX.Element {
    const names = useIntegrationPreviewNames("tana", requestId);
    const node = (id: string) => <TanaNodeLink id={id} name={names?.get(id)} />;
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

// Tana Desktop emits the renderer's confirmation followed by labeled IDs and placement.
const moveResultPattern =
  /^Moved "(.*)\n\nNode ID: (\S+)\nPrevious parent: (.*) \((\S+)\)\nNew parent: (.*) \((\S+)\)\nPosition: (start|end|before|after)$/;

const moveResult = z.string().transform((text, ctx) => {
  const match = moveResultPattern.exec(text);
  if (match) {
    const [, message, nodeId, previousName, previousId, targetName, targetId, position] = match;
    const suffix = `" from "${previousName}" to "${targetName}" (${position})`;
    // The first line repeats the parent names and placement. Match them before extracting the node title.
    if (message?.endsWith(suffix) && nodeId && previousId && targetId && position) {
      return {
        nodeId,
        nodeName: message.slice(0, -suffix.length),
        previousId,
        previousName,
        targetId,
        targetName,
        position,
      };
    }
  }
  ctx.addIssue({ code: "custom", message: "Unrecognized Tana move receipt" });
  return z.NEVER;
});

const moveReceipt = defineResultPreview(moveResult, ({ result }) => (
  <Text size="sm" style={{ overflowWrap: "anywhere" }}>
    Moved <TanaNodeLink id={result.nodeId} name={result.nodeName} /> from{" "}
    <TanaNodeLink id={result.previousId} name={result.previousName} /> to{" "}
    <TanaNodeLink id={result.targetId} name={result.targetName} /> ({result.position}).
  </Text>
));

const moveNodePresentation: ActionPresentation = {
  fallbackLabel: "Move Tana node",
  pane: { opened: moveNode },
  details: { arguments: moveNode, result: moveReceipt },
};

export const tanaRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  ["get_or_create_calendar_node", calendarNodePresentation],
  ["move_node", moveNodePresentation],
]);
