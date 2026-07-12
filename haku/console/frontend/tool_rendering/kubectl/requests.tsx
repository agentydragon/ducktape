// Per-tool-type rendering for the remote `kubectl-passthrough-mcp` MCP server (see
// cluster/k8s/agents/kubectl-passthrough-mcp/). Falls back to the generic raw-JSON view
// for anything that isn't shaped as expected — same caveat as gmail/requests.tsx:
// arguments are only validated by the tool's own schema at execution time, not at
// submission. Every tool here runs with the approving operator's own cluster-admin
// identity (cluster_auth_mode=passthrough) once approved, so rendering the exact target
// unambiguously matters more than for narrower-scoped tools.

import { Group, Stack } from "@mantine/core";
import { z } from "zod";

import { Field } from "../../field.tsx";
import { definePreview, type ToolPreview } from "../entry.tsx";
import { clampBlock, PreviewText, type PreviewProps } from "../vocabulary.tsx";

export const KUBECTL_SERVER_ID = "kubectl-passthrough-mcp";

// kubectl-passthrough-mcp is a remote third-party binary
// (containers/kubernetes-mcp-server), so its tools/list schemas are not available to the
// build-time in-process catalog. These are hand-authored against the live advertised schema.
const zResourcesCreateOrUpdateArgs = z.object({
  resource: z.string(),
});

const zResourcesDeleteArgs = z.object({
  apiVersion: z.string(),
  kind: z.string(),
  name: z.string(),
  namespace: z.string().optional(),
  gracePeriodSeconds: z.number().optional(),
});

const zPodsDeleteArgs = z.object({
  name: z.string(),
  namespace: z.string().optional(),
});
const zPodsLogArgs = z.object({
  name: z.string(),
  namespace: z.string().optional(),
  container: z.string().optional(),
  previous: z.boolean().optional(),
  tail: z.number().int().optional(),
});

type ResourcesCreateOrUpdateArgs = z.infer<typeof zResourcesCreateOrUpdateArgs>;
type ResourcesDeleteArgs = z.infer<typeof zResourcesDeleteArgs>;
type PodsDeleteArgs = z.infer<typeof zPodsDeleteArgs>;
type PodsLogArgs = z.infer<typeof zPodsLogArgs>;

function ResourcesApplyPreview({ args, variant }: PreviewProps<ResourcesCreateOrUpdateArgs>) {
  const resource = variant === "compact" ? clampBlock(args.resource, 3) : args.resource;
  return <pre className="haku-shell-json">{resource}</pre>;
}

function DeleteTargetPreview({
  kind,
  name,
  namespace,
  gracePeriodSeconds,
}: {
  kind: string;
  name: string;
  namespace: string | undefined;
  gracePeriodSeconds: number | undefined;
}) {
  return (
    <Stack gap="xs">
      <Group gap={4} className="haku-shell-mono">
        <PreviewText span fw={600}>
          {kind}
        </PreviewText>
        <PreviewText span>{name}</PreviewText>
        {namespace && (
          <PreviewText span c="dimmed">
            in {namespace}
          </PreviewText>
        )}
      </Group>
      {gracePeriodSeconds === 0 && (
        <PreviewText c="red">Immediate deletion (grace period 0) — no termination grace.</PreviewText>
      )}
    </Stack>
  );
}

function ResourcesDeletePreview({ args }: PreviewProps<ResourcesDeleteArgs>) {
  return (
    <DeleteTargetPreview
      kind={args.kind}
      name={args.name}
      namespace={args.namespace}
      gracePeriodSeconds={args.gracePeriodSeconds}
    />
  );
}

function PodsDeletePreview({ args }: PreviewProps<PodsDeleteArgs>) {
  return <DeleteTargetPreview kind="Pod" name={args.name} namespace={args.namespace} gracePeriodSeconds={undefined} />;
}

function PodsLogPreview({ args }: PreviewProps<PodsLogArgs>) {
  return (
    <Stack gap="xs">
      <Field label="Pod" mono>
        {args.namespace ? `${args.namespace}/` : ""}
        {args.name}
        {args.container ? ` · ${args.container}` : ""}
      </Field>
      <PreviewText c="dimmed">
        {args.previous ? "Previous container logs" : "Current logs"} · last {args.tail ?? 100} lines
      </PreviewText>
    </Stack>
  );
}

/** Per-tool preview widgets for the `kubectl-passthrough-mcp` server's highest-stakes tools
 * (apply and delete). */
export const kubectlPreviews = {
  resources_create_or_update: definePreview(zResourcesCreateOrUpdateArgs, ResourcesApplyPreview, () => ({
    text: "kubectl: Apply resource",
  })),
  resources_delete: definePreview(zResourcesDeleteArgs, ResourcesDeletePreview, (a) => ({
    text: `kubectl: Delete ${a.kind}`,
    destructive: true,
  })),
  pods_delete: definePreview(zPodsDeleteArgs, PodsDeletePreview, () => ({
    text: "kubectl: Delete Pod",
    destructive: true,
  })),
  pods_log: definePreview(zPodsLogArgs, PodsLogPreview, () => ({ text: "kubectl: View Pod logs" })),
} satisfies Record<string, ToolPreview>;
