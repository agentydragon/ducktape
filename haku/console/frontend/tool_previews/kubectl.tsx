// Per-tool-type rendering for the remote `kubectl-passthrough-mcp` MCP server (see
// cluster/k8s/agents/kubectl-passthrough-mcp/). Falls back to the generic raw-JSON view
// for anything that isn't shaped as expected — same caveat as gmail.tsx:
// arguments are only validated by the tool's own schema at execution time, not at
// submission. Every tool here runs with the approving operator's own cluster-admin
// identity (cluster_auth_mode=passthrough) once approved, so rendering the exact target
// unambiguously matters more than for narrower-scoped tools.

import { Badge, Group, Stack, Text } from "@mantine/core";
import { z } from "zod";

import { Field } from "../field.tsx";
import { definePreview, type ToolPreview } from "./entry.tsx";
import { clampBlock, type PreviewVariant } from "./variant.tsx";

export const KUBECTL_SERVER_ID = "kubectl-passthrough-mcp";

// kubectl-passthrough-mcp is a third-party binary (containers/kubernetes-mcp-server) —
// there's no backend Pydantic model to generate these from (unlike gmail.tsx's
// :schema_zod), so they're hand-authored once, here, against that tool's real input schema
// (checked via a live `tools/list` call against the deployed server).
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

type ResourcesCreateOrUpdateArgs = z.infer<typeof zResourcesCreateOrUpdateArgs>;
type ResourcesDeleteArgs = z.infer<typeof zResourcesDeleteArgs>;
type PodsDeleteArgs = z.infer<typeof zPodsDeleteArgs>;

function ResourcesApplyPreview({ args, variant }: { args: ResourcesCreateOrUpdateArgs; variant: PreviewVariant }) {
  const resource = variant === "compact" ? clampBlock(args.resource, 3) : args.resource;
  return (
    <Stack gap="xs">
      <Field label="Action">
        <Badge color="blue" variant="light">
          Apply
        </Badge>
      </Field>
      <Field label="Resource">
        <pre className="haku-shell-json">{resource}</pre>
      </Field>
    </Stack>
  );
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
      <Field label="Action">
        <Badge color="red" variant="filled">
          Delete
        </Badge>
      </Field>
      <Field label="Target" mono>
        <Group gap={4}>
          <Text span fw={600}>
            {kind}
          </Text>
          <Text span>{name}</Text>
          {namespace && (
            <Text span c="dimmed">
              in {namespace}
            </Text>
          )}
        </Group>
      </Field>
      {gracePeriodSeconds === 0 && (
        <Text size="sm" c="red">
          Immediate deletion (grace period 0) — no termination grace.
        </Text>
      )}
    </Stack>
  );
}

function ResourcesDeletePreview({ args }: { args: ResourcesDeleteArgs }) {
  return (
    <DeleteTargetPreview
      kind={args.kind}
      name={args.name}
      namespace={args.namespace}
      gracePeriodSeconds={args.gracePeriodSeconds}
    />
  );
}

function PodsDeletePreview({ args }: { args: PodsDeleteArgs }) {
  return <DeleteTargetPreview kind="Pod" name={args.name} namespace={args.namespace} gracePeriodSeconds={undefined} />;
}

/** Per-tool preview widgets for the `kubectl-passthrough-mcp` server's highest-stakes tools
 * (apply and delete). */
export const kubectlPreviews = {
  resources_create_or_update: definePreview(zResourcesCreateOrUpdateArgs, (args, variant) => (
    <ResourcesApplyPreview args={args} variant={variant} />
  )),
  resources_delete: definePreview(zResourcesDeleteArgs, (args) => <ResourcesDeletePreview args={args} />),
  pods_delete: definePreview(zPodsDeleteArgs, (args) => <PodsDeletePreview args={args} />),
} satisfies Record<string, ToolPreview>;
