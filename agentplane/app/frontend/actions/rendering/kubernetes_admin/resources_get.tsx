import type { JSX } from "react";
import { Code, Text } from "@mantine/core";
import { z } from "zod";

import { Chip } from "../chips";
import { definePreview, type ArgumentsPreview } from "../entry";

import { kubernetesTarget, resource } from "./resource_common";

const resourcesGetArguments = z.strictObject({ ...resource, namespace: z.string().min(1).optional() });

export function canQuickApproveResourcesGet(args: unknown): boolean {
  return resourcesGetArguments.safeParse(args).success;
}
function GetPane({ args }: { args: z.infer<typeof resourcesGetArguments> }): JSX.Element {
  return <Chip label="API version" value={args.apiVersion} />;
}

function GetLabel({ args }: { args: z.infer<typeof resourcesGetArguments> }): JSX.Element {
  return (
    <Text size="sm" fw={600}>
      Get <Code>{kubernetesTarget(args.kind, args.name, args.namespace)}</Code>
    </Text>
  );
}

export const resourcesGetLabel: ArgumentsPreview = definePreview(resourcesGetArguments, GetLabel);
export const resourcesGetPane: ArgumentsPreview = definePreview(resourcesGetArguments, GetPane);
