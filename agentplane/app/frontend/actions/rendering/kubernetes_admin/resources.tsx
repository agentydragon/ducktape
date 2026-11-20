import { Code, Paper, Stack, Text } from "@mantine/core";
import type { JSX } from "react";
import { stringify } from "yaml";
import { z } from "zod";

import { Disclosure, DisclosureBody } from "../../../../../../util/frontend/disclosure";
import { TextBlock } from "../../../ui/text_block";
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

const controllerKinds = new Set(["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"]);

const resourceStatus = z.object({
  phase: z.string().optional(),
  reason: z.string().optional(),
  conditions: z.array(z.object({ type: z.string(), status: z.string(), reason: z.string().optional() })).optional(),
  containerStatuses: z
    .array(
      z.object({
        ready: z.boolean(),
        restartCount: z.int().optional(),
        state: z
          .object({
            waiting: z.object({ reason: z.string().optional() }).optional(),
            terminated: z.object({ reason: z.string().optional() }).optional(),
          })
          .optional(),
      })
    )
    .optional(),
  readyReplicas: z.int().optional(),
  numberReady: z.int().optional(),
  desiredNumberScheduled: z.int().optional(),
});

function observedStatus(resource: KubernetesResource): string[] {
  const parsed = resourceStatus.safeParse(resource.status);
  if (!parsed.success) return [];
  const status = parsed.data;
  const known = [status.phase, status.reason].filter((value): value is string => Boolean(value));
  if (resource.kind === "Pod") {
    const containers = status.containerStatuses;
    const ready = status.conditions?.find((condition) => condition.type === "Ready");
    if (ready?.status === "True") known.push("Ready");
    if (ready?.status === "False") known.push("Not ready");
    if (containers && containers.length > 0) {
      known.push(
        `${containers.filter((container) => container.ready).length}/${containers.length} reported containers ready`
      );
      if (containers.every((container) => container.restartCount !== undefined)) {
        known.push(`${containers.reduce((sum, container) => sum + (container.restartCount ?? 0), 0)} restarts`);
      }
      for (const container of containers) {
        const reason = container.state?.waiting?.reason ?? container.state?.terminated?.reason;
        if (reason) known.push(reason);
      }
    }
  } else if (typeof resource.kind === "string" && controllerKinds.has(resource.kind)) {
    const ready = status.readyReplicas ?? status.numberReady;
    const spec = z.object({ replicas: z.int().optional() }).safeParse(resource.spec);
    const desired = status.desiredNumberScheduled ?? (spec.success ? spec.data.replicas : undefined);
    if (ready !== undefined) known.push(desired === undefined ? `${ready} ready` : `${ready}/${desired} ready`);
  }
  return [...new Set(known)];
}

export function KubernetesResourceSummary({ resource }: { resource: KubernetesResource }): JSX.Element {
  const observations = observedStatus(resource);
  return (
    <Paper withBorder p="xs">
      <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
        <Code>{kubernetesResourceTarget(resource)}</Code>
      </Text>
      {observations.length > 0 && <Text size="sm">{observations.join(" · ")}</Text>}
    </Paper>
  );
}

export function KubernetesResourceResult({ resources }: { resources: KubernetesResource[] }): JSX.Element {
  return (
    <Stack gap="xs">
      {resources.map((resource, index) => (
        <KubernetesResourceSummary key={index} resource={resource} />
      ))}
      <Disclosure summary={<Text size="sm">Resource YAML</Text>}>
        <DisclosureBody>
          <KubernetesResources resources={resources} heading="Resource" />
        </DisclosureBody>
      </Disclosure>
    </Stack>
  );
}

export const kubernetesResourcesResult: ResultPreview = defineResultPreview(kubernetesResourcesSchema, ({ result }) => (
  <KubernetesResourceResult resources={result} />
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
