import type { JSX } from "react";
import { Code, Text } from "@mantine/core";
import { z } from "zod";

import type { ActionPresentation } from "../presentation";
import { definePreview, type ArgumentsPreview } from "../entry";

import { kubernetesTarget, resource } from "./resource_common";
import { kubernetesResourcesResult } from "./resources";

const resourcesGetArguments = z.strictObject({ ...resource, namespace: z.string().min(1).optional() });

export function offersCardDecisionButtonsResourcesGet(args: unknown): boolean {
  return resourcesGetArguments.safeParse(args).success;
}
function GetSummary({ args }: { args: z.infer<typeof resourcesGetArguments> }): JSX.Element {
  return (
    <Text size="sm">
      Read with API version <Code>{args.apiVersion}</Code>
    </Text>
  );
}

function GetLabel({ args }: { args: z.infer<typeof resourcesGetArguments> }): JSX.Element {
  return (
    <Text size="sm" fw={600}>
      Get <Code>{kubernetesTarget(args.kind, args.name, args.namespace)}</Code>
    </Text>
  );
}

export const resourcesGetLabel: ArgumentsPreview = definePreview(resourcesGetArguments, GetLabel);
export const resourcesGetSummary: ArgumentsPreview = definePreview(resourcesGetArguments, GetSummary);

export const resourcesGetPresentation: ActionPresentation = {
  fallbackLabel: "Get Kubernetes resource",
  label: resourcesGetLabel,
  pane: { collapsed: resourcesGetSummary, opened: resourcesGetSummary },
  details: { arguments: resourcesGetSummary, result: kubernetesResourcesResult },
  offersCardDecisionButtons: offersCardDecisionButtonsResourcesGet,
};
