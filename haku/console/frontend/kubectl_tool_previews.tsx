// Per-tool-type rendering for the remote `kubectl-passthrough-mcp` MCP server (see
// cluster/k8s/agents/kubectl-passthrough-mcp/). Falls back to the generic raw-JSON view
// for anything that isn't shaped as expected — same caveat as google_tool_previews.tsx:
// arguments are only validated by the tool's own schema at execution time, not at
// submission. Every tool here runs with the approving operator's own cluster-admin
// identity (cluster_auth_mode=passthrough) once approved, so rendering the exact target
// unambiguously matters more than for narrower-scoped tools.

import { Badge, Group, Stack, Text } from "@mantine/core";
import type { ReactNode } from "react";

import { Field } from "./field.tsx";

const KUBECTL_SERVER_ID = "kubectl-passthrough-mcp";

interface ResourcesCreateOrUpdateArgs {
  resource: string;
}

interface ResourcesDeleteArgs {
  apiVersion: string;
  kind: string;
  name: string;
  namespace?: string;
  gracePeriodSeconds?: number;
}

interface PodsDeleteArgs {
  name: string;
  namespace?: string;
}

function asResourcesCreateOrUpdateArgs(args: Record<string, unknown>): ResourcesCreateOrUpdateArgs | null {
  if (typeof args.resource !== "string") return null;
  return { resource: args.resource };
}

function asResourcesDeleteArgs(args: Record<string, unknown>): ResourcesDeleteArgs | null {
  if (typeof args.apiVersion !== "string" || typeof args.kind !== "string" || typeof args.name !== "string") {
    return null;
  }
  if (args.namespace !== undefined && typeof args.namespace !== "string") return null;
  if (args.gracePeriodSeconds !== undefined && typeof args.gracePeriodSeconds !== "number") return null;
  return args as unknown as ResourcesDeleteArgs;
}

function asPodsDeleteArgs(args: Record<string, unknown>): PodsDeleteArgs | null {
  if (typeof args.name !== "string") return null;
  if (args.namespace !== undefined && typeof args.namespace !== "string") return null;
  return args as unknown as PodsDeleteArgs;
}

function ResourcesApplyPreview({ args }: { args: ResourcesCreateOrUpdateArgs }) {
  return (
    <Stack gap="xs">
      <Field label="Action">
        <Badge color="blue" variant="light">
          Apply
        </Badge>
      </Field>
      <Field label="Resource">
        <pre className="haku-shell-json">{args.resource}</pre>
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

/** Nice per-tool rendering for the `kubectl-passthrough-mcp` server's highest-stakes tools
 * (apply and delete); `null` for anything else, so the caller falls back to raw JSON. */
export function kubectlToolPreview(
  serverId: string,
  toolName: string,
  args: Record<string, unknown>
): ReactNode | null {
  if (serverId !== KUBECTL_SERVER_ID) return null;
  if (toolName === "resources_create_or_update") {
    const parsed = asResourcesCreateOrUpdateArgs(args);
    return parsed && <ResourcesApplyPreview args={parsed} />;
  }
  if (toolName === "resources_delete") {
    const parsed = asResourcesDeleteArgs(args);
    return parsed && <ResourcesDeletePreview args={parsed} />;
  }
  if (toolName === "pods_delete") {
    const parsed = asPodsDeleteArgs(args);
    return parsed && <PodsDeletePreview args={parsed} />;
  }
  return null;
}
