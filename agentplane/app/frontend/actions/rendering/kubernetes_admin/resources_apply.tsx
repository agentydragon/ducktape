import { Code, Stack, Text } from "@mantine/core";
import { Fragment, type JSX } from "react";
import { z } from "zod";

import type { ActionPresentation } from "../presentation";
import { definePreview, type ArgumentsPreview } from "../entry";
import { KubernetesManifestText, kubernetesResourcesResult } from "./resources";
import {
  type KubernetesResource,
  kubernetesResourceTarget,
  parseKubernetesYaml,
} from "../../schemas/kubernetes_admin/manifest";

const resourceApplyArguments = z.strictObject({ resource: z.string().min(1) });

// Malformed or non-resource YAML fails here, so the label falls back and the manifest is shown verbatim.
const appliedResources = resourceApplyArguments.transform((args, context) => {
  const resources = parseKubernetesYaml(args.resource);
  if (resources === undefined) {
    context.addIssue({ code: "custom", message: "Expected Kubernetes resource document(s)" });
    return z.NEVER;
  }
  return resources;
});

const IDENTITY_FIELDS = new Set(["apiVersion", "kind", "metadata.name", "metadata.namespace"]);
const MAX_LISTED_FIELDS = 3;
const MAX_LISTED_VALUE_LENGTH = 80;

type Leaf = { path: string; value: unknown };

function leaves(value: unknown, path: string): Leaf[] {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return [{ path, value }];
  const entries = Object.entries(value);
  if (entries.length === 0) return [{ path, value }];
  return entries.flatMap(([key, child]) => leaves(child, path === "" ? key : `${path}.${key}`));
}

function listedValue(value: unknown): string | undefined {
  if (typeof value === "string") {
    return value.includes("\n") || value.length > MAX_LISTED_VALUE_LENGTH ? undefined : value;
  }
  return value === null || typeof value === "number" || typeof value === "boolean" ? String(value) : undefined;
}

/** `metadata` groups by its second segment (`metadata.labels`); everything else by its top-level field. */
function fieldGroup(path: string): string {
  const [top, second] = path.split(".");
  return top === "metadata" && second !== undefined ? `${top}.${second}` : top;
}

/** What one applied resource sets beyond its identity: each field when a few short ones, else a count. */
function resourceFields(resource: KubernetesResource): JSX.Element | null {
  const fields = leaves(resource, "").filter(({ path }) => !IDENTITY_FIELDS.has(path));
  if (fields.length === 0) return null;
  const listed = fields.map(({ path, value }) => ({ path, text: listedValue(value) }));
  if (listed.length <= MAX_LISTED_FIELDS && listed.every(({ text }) => text !== undefined)) {
    return (
      <Stack gap={2}>
        {listed.map(({ path, text }) => (
          <Text key={path} size="xs" ff="monospace" style={{ overflowWrap: "anywhere" }}>
            {path}: {text}
          </Text>
        ))}
      </Stack>
    );
  }
  const groups = [...new Set(fields.map(({ path }) => fieldGroup(path)))];
  return (
    <Text size="sm" style={{ overflowWrap: "anywhere" }}>
      Sets {fields.length} {fields.length === 1 ? "field" : "fields"} under {groups.join(", ")}
    </Text>
  );
}

function ApplyLabel({ args }: { args: KubernetesResource[] }): JSX.Element {
  if (args.length === 1) {
    return (
      <Text size="sm" fw={600}>
        Apply <Code>{kubernetesResourceTarget(args[0])}</Code>
      </Text>
    );
  }
  const remaining = args.length - 2;
  return (
    <Text size="sm" fw={600}>
      Apply {args.length} Kubernetes resources:{" "}
      {args.slice(0, 2).map((resource, index) => (
        <Fragment key={index}>
          {index > 0 && ", "}
          <Code>{kubernetesResourceTarget(resource)}</Code>
        </Fragment>
      ))}
      {remaining > 0 && `, and ${remaining} more`}
    </Text>
  );
}

function Manifest({ args }: { args: z.infer<typeof resourceApplyArguments> }): JSX.Element {
  return <KubernetesManifestText text={args.resource} />;
}

// Several resources are named in the label alone. Direct previews preserve null as the slot
// suppression signal after schema validation.
export const resourcesApplyCollapsed: ArgumentsPreview = {
  schema: appliedResources,
  render: (resources: KubernetesResource[]) => (resources.length === 1 ? resourceFields(resources[0]) : null),
};
export const resourcesApplyManifest: ArgumentsPreview = definePreview(resourceApplyArguments, Manifest);
export const resourcesApplyLabel: ArgumentsPreview = definePreview(appliedResources, ApplyLabel);

export const resourcesApplyPresentation: ActionPresentation = {
  fallbackLabel: "Apply Kubernetes resource",
  label: resourcesApplyLabel,
  pane: { collapsed: resourcesApplyCollapsed, opened: resourcesApplyManifest },
  details: { arguments: resourcesApplyManifest, result: kubernetesResourcesResult },
};
