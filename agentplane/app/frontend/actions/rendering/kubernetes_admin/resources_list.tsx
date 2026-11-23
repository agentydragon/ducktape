import { resourceListResult } from "./list_result";
import type { JSX } from "react";
import { Code, Text } from "@mantine/core";
import { z } from "zod";

import { type ActionPresentation, expandsForDetails } from "../presentation";
import { definePreview, type ArgumentsPreview } from "../entry";

import { resource } from "./resource_common";

const listSelectors = {
  fieldSelector: z.string().min(1).optional(),
  labelSelector: z.string().min(1).optional(),
};
const resourcesListArguments = z.strictObject({
  apiVersion: resource.apiVersion,
  kind: resource.kind,
  namespace: resource.namespace,
  ...listSelectors,
});

export function offersCardDecisionButtonsResourcesList(args: unknown): boolean {
  return resourcesListArguments.safeParse(args).success;
}

function ListOptions({
  args,
  compact = false,
}: {
  args: z.infer<typeof resourcesListArguments>;
  compact?: boolean;
}): JSX.Element {
  const selectors: string[] = [];
  if (args.fieldSelector) selectors.push(`field selector ${args.fieldSelector}`);
  if (args.labelSelector) selectors.push(`label selector ${args.labelSelector}`);
  return (
    <Text size="sm" style={{ overflowWrap: "anywhere" }}>
      Using API version <Code>{args.apiVersion}</Code>
      {!compact && selectors.length > 0 ? ` · matching ${selectors.join(" and ")}` : ""}.
    </Text>
  );
}

function ListLabel({ args }: { args: z.infer<typeof resourcesListArguments> }): JSX.Element {
  return (
    <Text size="sm" fw={600}>
      List <Code>{args.kind}</Code> resources
      {args.namespace && (
        <>
          {" "}
          in namespace <Code>{args.namespace}</Code>
        </>
      )}
    </Text>
  );
}

export const resourcesListLabel: ArgumentsPreview = definePreview(resourcesListArguments, ListLabel);
export const resourcesListPane: ArgumentsPreview = definePreview(resourcesListArguments, ListOptions);
const resourcesListSummary: ArgumentsPreview = definePreview(resourcesListArguments, ({ args }) => (
  <ListOptions args={args} compact />
));

export const resourcesListPresentation: ActionPresentation = {
  label: resourcesListLabel,
  pane: { collapsed: resourcesListSummary, opened: resourcesListPane },
  details: { arguments: resourcesListPane, envelopeResult: resourceListResult },
  offersCardDecisionButtons: offersCardDecisionButtonsResourcesList,
  compactCardExpands: expandsForDetails(
    resourcesListArguments,
    (args) => args.fieldSelector !== undefined || args.labelSelector !== undefined
  ),
};
