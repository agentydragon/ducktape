import { Stack } from "@mantine/core";
import type { JSX } from "react";
import { stringify } from "yaml";
import { z } from "zod";

import { TextBlock } from "../../../text_block";
import { defineResultPreview, type ResultPreview } from "../result_entry";
import {
  isKubernetesResource,
  type KubernetesResource,
  kubernetesResourceTarget,
  parseKubernetesYaml,
} from "../../schemas/kubernetes_admin/manifest";

/** Decode a structured resource or a sequence of them; arbitrary objects stay on the generic path. */
export function kubernetesResources(value: unknown): KubernetesResource[] | undefined {
  if (typeof value === "string") return parseKubernetesYaml(value);
  const resources = Array.isArray(value) ? value.flatMap((item) => (Array.isArray(item) ? item : [item])) : [value];
  return resources.length > 0 && resources.every(isKubernetesResource) ? resources : undefined;
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
            heading={`${heading}${resources.length > 1 ? ` ${index + 1}` : ""} · ${kubernetesResourceTarget(resource)}`}
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
