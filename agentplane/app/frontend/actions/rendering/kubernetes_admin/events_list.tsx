import type { JSX } from "react";
import { Badge, Code, Group, Paper, Stack, Text } from "@mantine/core";
import { parseDocument } from "yaml";
import { z } from "zod";

import { type ActionPresentation, expandsForRejectedArguments } from "../presentation";
import { definePreview, type ArgumentsPreview } from "../entry";
import { defineResultPreview } from "../result_entry";
import { kubernetesTarget } from "../../schemas/kubernetes_admin/target";

const eventsListArguments = z.strictObject({
  namespace: z.string().min(1).optional(),
  fieldSelector: z.string().min(1).optional(),
});

export function offersCardDecisionButtonsEventsList(args: unknown): boolean {
  return eventsListArguments.safeParse(args).success;
}

function Events({ fieldSelector }: { fieldSelector: string }): JSX.Element {
  return (
    <Text size="sm" style={{ overflowWrap: "anywhere" }}>
      Show events matching <Code>{fieldSelector}</Code>.
    </Text>
  );
}

function EventsLabel({ args }: { args: z.infer<typeof eventsListArguments> }): JSX.Element {
  return (
    <Text size="sm" fw={600}>
      List events
      {args.namespace && (
        <>
          {" "}
          in namespace <Code>{args.namespace}</Code>
        </>
      )}
      {args.namespace === undefined && " across all namespaces"}
    </Text>
  );
}

export const eventsListLabel: ArgumentsPreview = definePreview(eventsListArguments, EventsLabel);
// Without a field selector the label is the whole call; direct previews preserve null as the slot
// suppression signal after schema validation.
export const eventsListPane: ArgumentsPreview = {
  schema: eventsListArguments,
  render: (args: z.infer<typeof eventsListArguments>) =>
    args.fieldSelector === undefined ? null : <Events fieldSelector={args.fieldSelector} />,
};

const eventRows = z.array(
  z.object({
    Namespace: z.string(),
    Timestamp: z.string(),
    Type: z.string(),
    Reason: z.string(),
    Message: z.string(),
    InvolvedObject: z.object({ Kind: z.string(), Name: z.string() }),
  })
);

const eventsResult = defineResultPreview(
  z.string().transform((text, context) => {
    if (text === "# No events found") return [];
    if (text.startsWith("# The following events (YAML format) were found:\n")) {
      const document = parseDocument(text, { prettyErrors: false, uniqueKeys: true });
      if (document.errors.length === 0) {
        try {
          const parsed = eventRows.safeParse(document.toJS());
          if (parsed.success) return parsed.data;
        } catch {
          // Leave unsupported output available in the generic receipt.
        }
      }
    }
    context.addIssue({ code: "custom", message: "Expected the Kubernetes event report" });
    return z.NEVER;
  }),
  ({ result }) => (
    <Stack gap="xs">
      {result.length === 0 && (
        <Text size="sm" c="dimmed">
          No events found.
        </Text>
      )}
      {result.map(({ Namespace, Timestamp, Type, Reason, Message, InvolvedObject }, index) => (
        <Paper key={index} p="xs" withBorder>
          <Group gap="xs" justify="space-between" align="flex-start">
            <Text fw={600} size="sm" style={{ overflowWrap: "anywhere" }}>
              <Code>{kubernetesTarget(InvolvedObject.Kind, InvolvedObject.Name, Namespace)}</Code>
            </Text>
            {Type && (
              <Badge size="sm" variant="light" color={Type === "Warning" ? "orange" : "gray"}>
                {Type}
              </Badge>
            )}
          </Group>
          {Reason && (
            <Text fw={600} size="sm">
              {Reason}
            </Text>
          )}
          {Message && (
            <Text size="sm" style={{ overflowWrap: "anywhere" }}>
              {Message}
            </Text>
          )}
          {Timestamp && !Timestamp.startsWith("0001-01-01 ") && (
            <Text size="xs" c="dimmed">
              {Timestamp}
            </Text>
          )}
        </Paper>
      ))}
    </Stack>
  )
);

export const eventsListPresentation: ActionPresentation = {
  label: eventsListLabel,
  pane: { collapsed: eventsListPane },
  details: { arguments: eventsListPane, result: eventsResult },
  offersCardDecisionButtons: offersCardDecisionButtonsEventsList,
  compactCardExpands: expandsForRejectedArguments(eventsListArguments),
};
