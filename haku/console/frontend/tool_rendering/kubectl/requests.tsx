// Per-tool-type rendering for the remote `kubectl-passthrough-mcp` MCP server (see
// cluster/k8s/agents/kubectl-passthrough-mcp/). Anything not shaped as expected falls back to the
// generic raw-JSON view: as with gmail/requests.tsx, arguments are validated at execution time, not
// at submission. Every approved call runs with the operator's own cluster-admin identity
// (cluster_auth_mode=passthrough), so rendering the exact target unambiguously matters more here
// than for narrower-scoped tools.

import { Group, Stack } from "@mantine/core";
import type { z } from "zod";

import { CodeBlock } from "../../code_block";
import { Field } from "../../field";
import { definePreview, type ToolPreview } from "../entry";
import { PreviewText, PreviewTitle, type PreviewProps } from "../vocabulary";
import {
  zPodsDeleteArgs,
  zPodsExecArgs,
  zPodsListInNamespaceArgs,
  zPodsLogArgs,
  zResourcesCreateOrUpdateArgs,
  zResourcesDeleteArgs,
  zResourcesGetArgs,
} from "./schemas";

type ResourcesCreateOrUpdateArgs = z.infer<typeof zResourcesCreateOrUpdateArgs>;
type ResourcesGetArgs = z.infer<typeof zResourcesGetArgs>;
type ResourcesDeleteArgs = z.infer<typeof zResourcesDeleteArgs>;
type PodsDeleteArgs = z.infer<typeof zPodsDeleteArgs>;
type PodsListInNamespaceArgs = z.infer<typeof zPodsListInNamespaceArgs>;
type PodsExecArgs = z.infer<typeof zPodsExecArgs>;
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

function ResourceTargetPreview({
  apiVersion,
  kind,
  name,
  namespace,
}: {
  apiVersion: string;
  kind: string;
  name: string;
  namespace: string | undefined;
}) {
  return (
    <Group gap={4} className="haku-shell-mono">
      <PreviewText span c="dimmed">
        {apiVersion}
      </PreviewText>
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
  );
}

function ResourcesGetPreview({ args }: PreviewProps<ResourcesGetArgs>) {
  return (
    <ResourceTargetPreview apiVersion={args.apiVersion} kind={args.kind} name={args.name} namespace={args.namespace} />
  );
}

function ResourcesDeletePreview({ args }: PreviewProps<ResourcesDeleteArgs>) {
  return (
    <Stack gap="xs">
      <ResourceTargetPreview
        apiVersion={args.apiVersion}
        kind={args.kind}
        name={args.name}
        namespace={args.namespace}
      />
      {args.gracePeriodSeconds === 0 && (
        <PreviewText c="red">Immediate deletion (grace period 0) — no termination grace.</PreviewText>
      )}
    </Stack>
  );
}

function PodsDeletePreview({ args }: PreviewProps<PodsDeleteArgs>) {
  return <ResourceTargetPreview apiVersion="v1" kind="Pod" name={args.name} namespace={args.namespace} />;
}

function PodsListInNamespacePreview({ args }: PreviewProps<PodsListInNamespaceArgs>) {
  return (
    <Stack gap="xs">
      <PreviewTitle className="haku-shell-mono">
        Pods
        <PreviewText span c="dimmed" fw={400}>
          in {args.namespace}
        </PreviewText>
      </PreviewTitle>
      {args.labelSelector && (
        <Field label="Label selector" mono>
          {args.labelSelector}
        </Field>
      )}
      {args.fieldSelector && (
        <Field label="Field selector" mono>
          {args.fieldSelector}
        </Field>
      )}
    </Stack>
  );
}

function shellQuote(arg: string): string {
  return /^[A-Za-z0-9_./:=,@+-]+$/.test(arg) ? arg : `'${arg.replaceAll("'", "'\\''")}'`;
}

function PodsExecPreview({ args, variant }: PreviewProps<PodsExecArgs>) {
  return (
    <Stack gap="xs">
      <Field label="Pod" mono>
        {args.namespace ? `${args.namespace}/` : ""}
        {args.name}
        {args.container ? ` · ${args.container}` : ""}
      </Field>
      <CodeBlock
        language="shell"
        value={args.command.map(shellQuote).join(" ")}
        compact={variant === "compact"}
        lineNumbers={variant === "detailed"}
      />
    </Stack>
  );
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

/** Per-tool preview widgets for the `kubectl-passthrough-mcp` server's highest-stakes tools. */
export const kubectlPreviews: {
  resources_create_or_update: ToolPreview<typeof zResourcesCreateOrUpdateArgs>;
  resources_get: ToolPreview<typeof zResourcesGetArgs>;
  resources_delete: ToolPreview<typeof zResourcesDeleteArgs>;
  pods_delete: ToolPreview<typeof zPodsDeleteArgs>;
  pods_list_in_namespace: ToolPreview<typeof zPodsListInNamespaceArgs>;
  pods_exec: ToolPreview<typeof zPodsExecArgs>;
  pods_log: ToolPreview<typeof zPodsLogArgs>;
} = {
  resources_create_or_update: definePreview(zResourcesCreateOrUpdateArgs, ResourcesApplyPreview),
  resources_get: definePreview(zResourcesGetArgs, ResourcesGetPreview),
  resources_delete: definePreview(zResourcesDeleteArgs, ResourcesDeletePreview),
  pods_delete: definePreview(zPodsDeleteArgs, PodsDeletePreview),
  pods_list_in_namespace: definePreview(zPodsListInNamespaceArgs, PodsListInNamespacePreview),
  pods_exec: definePreview(zPodsExecArgs, PodsExecPreview),
  pods_log: definePreview(zPodsLogArgs, PodsLogPreview),
} satisfies Record<string, ToolPreview>;
