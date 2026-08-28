import { Group, Stack } from "@mantine/core";
import type { z } from "zod";

import { Field } from "../../field";
import { mcpToolSchema, type McpToolArgumentsFor } from "../../mcp_tool_schema";
import { definePreview, type ToolPreview } from "../entry";
import { KUBERNETES_SERVER_ID } from "../server_ids";
import {
  COMPACT_ITEM_LIMIT,
  MoreLine,
  plural,
  PreviewBadge,
  PreviewText,
  PreviewTitle,
  type PreviewProps,
} from "../vocabulary";

export const zCreateGrantArgs: z.ZodType<McpToolArgumentsFor<typeof KUBERNETES_SERVER_ID, "create_grant">> =
  mcpToolSchema(KUBERNETES_SERVER_ID, "create_grant");
const zReleaseGrantsArgs: z.ZodType<McpToolArgumentsFor<typeof KUBERNETES_SERVER_ID, "release_grants">> = mcpToolSchema(
  KUBERNETES_SERVER_ID,
  "release_grants"
);

type CreateGrantArgs = z.infer<typeof zCreateGrantArgs>;
type ReleaseGrantsArgs = z.infer<typeof zReleaseGrantsArgs>;
export type GrantRule = {
  api_groups?: readonly string[];
  resources?: readonly string[];
  verbs: readonly string[];
  resource_names?: readonly string[];
  non_resource_urls?: readonly string[];
};
export type GrantScope =
  | { kind: "namespaces"; namespaces: readonly string[] }
  | { kind: "all_namespaces" }
  | { kind: "cluster" }
  | { kind: "non_resource" };

export type GrantShape = {
  scope: GrantScope;
  rules: readonly GrantRule[];
};

export function scopeLabel(scope: GrantScope): string {
  switch (scope.kind) {
    case "namespaces":
      return `Namespaces: ${scope.namespaces.join(", ")}`;
    case "all_namespaces":
      return "All namespaces";
    case "cluster":
      return "Cluster-scoped";
    case "non_resource":
      return "Non-resource URLs";
  }
}

function ruleTarget(rule: GrantRule): string {
  if (rule.non_resource_urls?.length) return rule.non_resource_urls.join(", ");
  const group = rule.api_groups?.filter(Boolean).join(", ");
  const resources = rule.resources?.join(", ") || "resources";
  const target = group ? `${group}/${resources}` : resources;
  return rule.resource_names?.length ? `${target} (${rule.resource_names.join(", ")})` : target;
}

function RuleLine({ rule }: { rule: GrantRule }) {
  return (
    <Group gap={4} wrap="wrap">
      <PreviewBadge variant="light" color="teal">
        {rule.verbs.join(", ")}
      </PreviewBadge>
      <PreviewText span className="haku-shell-mono">
        {ruleTarget(rule)}
      </PreviewText>
    </Group>
  );
}

export function GrantScopeAndRules({
  grant,
  variant,
}: {
  grant: GrantShape;
  variant: "compact" | "detailed";
}): JSX.Element {
  const rules = variant === "compact" ? grant.rules.slice(0, 1) : grant.rules;
  return (
    <Stack gap={4}>
      <PreviewTitle>{scopeLabel(grant.scope)}</PreviewTitle>
      <Stack gap={2}>
        {rules.map((rule, index) => (
          <RuleLine key={index} rule={rule} />
        ))}
        <MoreLine count={grant.rules.length - rules.length} />
      </Stack>
    </Stack>
  );
}

function formatDuration(seconds: number): string {
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds % 60 === 0) return `${seconds / 60}m`;
  return `${seconds}s`;
}

function CreateGrantPreview({ args, variant }: PreviewProps<CreateGrantArgs>) {
  const grants = variant === "compact" ? args.grants.slice(0, COMPACT_ITEM_LIMIT) : args.grants;
  return (
    <Stack gap="xs">
      <Group gap={6}>
        <PreviewTitle>{plural(args.grants.length, "temporary grant")}</PreviewTitle>
        <PreviewBadge variant="outline">for {formatDuration(args.duration_seconds)}</PreviewBadge>
        <PreviewBadge variant="light">
          applies to {args.applies_to === "session" ? "this session" : "the Agent"}
        </PreviewBadge>
      </Group>
      <Stack gap="xs">
        {grants.map((grant, index) => (
          <GrantScopeAndRules key={index} grant={grant} variant={variant} />
        ))}
        <MoreLine count={args.grants.length - grants.length} />
      </Stack>
    </Stack>
  );
}

function GrantIdList({ ids, variant }: { ids: readonly string[]; variant: "compact" | "detailed" }) {
  const shown = variant === "compact" ? ids.slice(0, COMPACT_ITEM_LIMIT) : ids;
  return (
    <Stack gap={2}>
      {shown.map((id) => (
        <PreviewText key={id} className="haku-shell-mono">
          {id}
        </PreviewText>
      ))}
      <MoreLine count={ids.length - shown.length} />
    </Stack>
  );
}

function ReleaseGrantsPreview({ args, variant }: PreviewProps<ReleaseGrantsArgs>) {
  return (
    <Stack gap="xs">
      <PreviewTitle>{plural(args.grant_ids.length, "grant")}</PreviewTitle>
      <GrantIdList ids={args.grant_ids} variant={variant} />
      <Field label="Reason">{args.reason ?? "released"}</Field>
    </Stack>
  );
}

export const kubernetesPreviews: {
  create_grant: ToolPreview<typeof zCreateGrantArgs>;
  release_grants: ToolPreview<typeof zReleaseGrantsArgs>;
} = {
  create_grant: definePreview(zCreateGrantArgs, CreateGrantPreview),
  release_grants: definePreview(zReleaseGrantsArgs, ReleaseGrantsPreview),
} satisfies Record<string, ToolPreview>;
