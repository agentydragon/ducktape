// Per-tool-type rendering for the remote `kubectl-passthrough-mcp` MCP server (see
// cluster/k8s/agents/kubectl-passthrough-mcp/). Falls back to the generic raw-JSON view
// for anything that isn't shaped as expected — same caveat as gmail/requests.tsx:
// arguments are only validated by the tool's own schema at execution time, not at
// submission. Every tool here runs with the approving operator's own cluster-admin
// identity (cluster_auth_mode=passthrough) once approved, so rendering the exact target
// unambiguously matters more than for narrower-scoped tools.

import { Group, Stack } from "@mantine/core";
import { z } from "zod";

import { CodeBlock } from "../../code_block";
import { Field } from "../../field";
import { definePreview, type ToolPreview } from "../entry";
import { PreviewText, type PreviewProps } from "../vocabulary";
import { KUBECTL_SERVER_ID } from "../server_ids";
import { zPodsDeleteArgs, zPodsLogArgs, zResourcesCreateOrUpdateArgs, zResourcesDeleteArgs } from "./schemas";

// kubectl-passthrough-mcp is a remote third-party binary
// (containers/kubernetes-mcp-server), so its tools/list schemas are not available to the
// build-time in-process catalog. These are hand-authored against the live advertised schema.
type ResourcesCreateOrUpdateArgs = z.infer<typeof zResourcesCreateOrUpdateArgs>;
type ResourcesDeleteArgs = z.infer<typeof zResourcesDeleteArgs>;
type PodsDeleteArgs = z.infer<typeof zPodsDeleteArgs>;
type PodsLogArgs = z.infer<typeof zPodsLogArgs>;

function ResourcesApplyPreview({ args, variant }: PreviewProps<ResourcesCreateOrUpdateArgs>) {
  return (
    <CodeBlock
      language="yaml"
      value={args.resource}
      compact={variant === "compact"}
      lineNumbers={variant === "detailed"}
    />
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
  resources_create_or_update: definePreview(zResourcesCreateOrUpdateArgs, ResourcesApplyPreview),
  resources_delete: definePreview(zResourcesDeleteArgs, ResourcesDeletePreview),
  pods_delete: definePreview(zPodsDeleteArgs, PodsDeletePreview),
  pods_log: definePreview(zPodsLogArgs, PodsLogPreview),
} satisfies Record<string, ToolPreview>;
