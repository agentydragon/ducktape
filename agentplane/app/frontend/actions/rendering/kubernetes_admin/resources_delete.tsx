import type { JSX } from "react";
import { Code, Text } from "@mantine/core";
import { z } from "zod";

import type { ActionPresentation } from "../presentation";
import { defineCallPreview, definePreview, type ArgumentsPreview, type CallPreview } from "../entry";
import type { ResultPreview } from "../result_entry";

import { kubernetesTarget, resource } from "./resource_common";

const resourcesDeleteArguments = z.strictObject({
  ...resource,
  gracePeriodSeconds: z.int().nonnegative().optional(),
});
const resourcesDeleteResult = z.literal("Resource deleted successfully");

function Delete({ args }: { args: z.infer<typeof resourcesDeleteArguments> }): JSX.Element {
  return (
    <Text size="sm">
      Use API version <Code>{args.apiVersion}</Code>
      {args.gracePeriodSeconds !== undefined && `; request a ${args.gracePeriodSeconds}s grace period`}
    </Text>
  );
}

function DeleteLabel({ args }: { args: z.infer<typeof resourcesDeleteArguments> }): JSX.Element {
  return (
    <Text size="sm" fw={600}>
      Delete <Code>{kubernetesTarget(args.kind, args.name, args.namespace)}</Code>
    </Text>
  );
}

export const resourcesDeleteLabel: ArgumentsPreview = definePreview(resourcesDeleteArguments, DeleteLabel);
export const resourcesDeletePane: ArgumentsPreview = definePreview(resourcesDeleteArguments, Delete);
export const resourcesDeleteCall: CallPreview = defineCallPreview(
  resourcesDeleteArguments,
  resourcesDeleteResult,
  ({ args }) => <Delete args={args} />
);
export const resourcesDeleteResultPreview: ResultPreview = {
  schema: resourcesDeleteResult,
  render: () => null,
};

export const resourcesDeletePresentation: ActionPresentation = {
  fallbackLabel: "Delete Kubernetes resource",
  outcome: (value) =>
    resourcesDeleteResult.safeParse(value).success ? { label: "Deletion accepted", failed: false } : undefined,
  label: resourcesDeleteLabel,
  pane: { opened: resourcesDeletePane },
  call: resourcesDeleteCall,
  details: { arguments: resourcesDeletePane, result: resourcesDeleteResultPreview },
};
