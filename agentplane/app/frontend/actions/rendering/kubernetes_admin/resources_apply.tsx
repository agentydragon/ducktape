import { Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import type { ActionPresentation } from "../presentation";
import { definePreview, type ArgumentsPreview } from "../entry";
import { KubernetesManifestText, kubernetesResourceSummary, kubernetesResourcesResult } from "./resources";

const resourceApplyArguments = z.strictObject({ resource: z.string().min(1) });

function Collapsed({ args }: { args: z.infer<typeof resourceApplyArguments> }): JSX.Element {
  return (
    <Text size="sm" style={{ overflowWrap: "anywhere" }}>
      {kubernetesResourceSummary(args.resource) ?? "Kubernetes manifest"}
    </Text>
  );
}

function ApplyLabel(): JSX.Element {
  return (
    <Text size="sm" fw={600}>
      Apply Kubernetes resource
    </Text>
  );
}

function Manifest({ args }: { args: z.infer<typeof resourceApplyArguments> }): JSX.Element {
  return <KubernetesManifestText text={args.resource} />;
}

export const resourcesApplyCollapsed: ArgumentsPreview = definePreview(resourceApplyArguments, Collapsed);
export const resourcesApplyManifest: ArgumentsPreview = definePreview(resourceApplyArguments, Manifest);
export const resourcesApplyLabel: ArgumentsPreview = definePreview(resourceApplyArguments, ApplyLabel);

export const resourcesApplyPresentation: ActionPresentation = {
  fallbackLabel: "Apply Kubernetes resource",
  label: resourcesApplyLabel,
  pane: { collapsed: resourcesApplyCollapsed, opened: resourcesApplyManifest },
  details: { arguments: resourcesApplyManifest, result: kubernetesResourcesResult },
};
