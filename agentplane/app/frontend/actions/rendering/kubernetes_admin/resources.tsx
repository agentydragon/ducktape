import { Stack } from "@mantine/core";
import type { JSX } from "react";
import { parseAllDocuments, stringify } from "yaml";
import { z } from "zod";

import { TextBlock } from "../../../text_block";
import { defineResultPreview, type ResultPreview } from "../result_entry";
import { kubernetesTarget } from "./resource_common";

export type KubernetesResource = Record<string, unknown>;

/** A value is a Kubernetes resource only when its identity fields have the standard API shape. */
export function isKubernetesResource(value: unknown): value is KubernetesResource {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const resource = value as Record<string, unknown>;
  if (typeof resource.apiVersion !== "string" || typeof resource.kind !== "string") return false;
  const metadata = resource.metadata;
  return metadata === undefined || (metadata !== null && typeof metadata === "object" && !Array.isArray(metadata));
}

/** Known Kubernetes YAML may be a document stream or a YAML sequence of resources. */
export function parseKubernetesYaml(text: string): KubernetesResource[] | undefined {
  try {
    const documents = parseAllDocuments(text, { prettyErrors: false, uniqueKeys: true });
    if (documents.some((document) => document.errors.length > 0)) return undefined;
    const values = documents.flatMap((document) => (document.contents === null ? [] : [document.toJS()]));
    const resources = values.flatMap((value) => (Array.isArray(value) ? value : [value]));
    return resources.length > 0 && resources.every(isKubernetesResource) ? resources : undefined;
  } catch {
    return undefined;
  }
}

/** Decode a structured resource or a sequence of them; arbitrary objects stay on the generic path. */
export function kubernetesResources(value: unknown): KubernetesResource[] | undefined {
  if (typeof value === "string") return parseKubernetesYaml(value);
  const resources = Array.isArray(value) ? value.flatMap((item) => (Array.isArray(item) ? item : [item])) : [value];
  return resources.length > 0 && resources.every(isKubernetesResource) ? resources : undefined;
}

/** A compact operation summary for a manifest; malformed or non-resource YAML has no inferred identity. */
export function kubernetesResourceSummary(text: string): string | undefined {
  const resources = parseKubernetesYaml(text);
  if (resources === undefined) return undefined;

  const identities = resources.map(resourceTarget);
  if (identities.length === 1) return `Apply ${identities[0]}`;

  const listed = identities.slice(0, 2).join(", ");
  const remaining = identities.length - 2;
  return `Apply ${identities.length} Kubernetes resources: ${listed}${remaining > 0 ? `, and ${remaining} more` : ""}`;
}

export const kubernetesResourcesSchema: z.ZodType<KubernetesResource[]> = z.unknown().transform((value, context) => {
  const resources = kubernetesResources(value);
  if (resources === undefined) {
    context.addIssue({ code: "custom", message: "Expected Kubernetes resource document(s)" });
    return z.NEVER;
  }
  return resources;
});

export const kubernetesResourcesResult: ResultPreview = defineResultPreview(kubernetesResourcesSchema, ({ result }) => (
  <KubernetesResources resources={result} heading="Resource" />
));

/** Render each resource as block YAML while Raw continues to expose the exact stored envelope. */
export function KubernetesResources({
  resources,
  heading,
}: {
  resources: readonly KubernetesResource[];
  heading: "Manifest" | "Resource";
}): JSX.Element {
  return (
    <Stack gap="sm">
      {resources.map((resource, index) => {
        const text = stringify(resource, { indent: 2, lineWidth: 0 });
        return (
          <KubernetesYamlText
            key={`${resource.apiVersion}-${resource.kind}-${index}`}
            heading={`${heading}${resources.length > 1 ? ` ${index + 1}` : ""} · ${resourceTarget(resource)}`}
            text={text}
          />
        );
      })}
    </Stack>
  );
}

/** Invalid or non-resource input is still shown verbatim in a YAML-aware editor. */
export function KubernetesManifestText({ text }: { text: string }): JSX.Element {
  const resources = parseKubernetesYaml(text);
  if (resources === undefined) {
    return <KubernetesYamlText heading="Manifest · showing submitted text" text={text} />;
  }
  return <KubernetesResources resources={resources} heading="Manifest" />;
}

function KubernetesYamlText({ heading, text }: { heading: string; text: string }): JSX.Element {
  return <TextBlock text={text} language="yaml" heading={heading} />;
}

function resourceTarget(resource: KubernetesResource): string {
  const metadata = resource.metadata;
  if (metadata === null || typeof metadata !== "object" || Array.isArray(metadata)) return String(resource.kind);
  const fields = metadata as Record<string, unknown>;
  const name = typeof fields.name === "string" && fields.name.length > 0 ? fields.name : undefined;
  const namespace = typeof fields.namespace === "string" && fields.namespace.length > 0 ? fields.namespace : undefined;
  return name === undefined ? String(resource.kind) : kubernetesTarget(String(resource.kind), name, namespace);
}
