import { Group, Stack } from "@mantine/core";
import type { z } from "zod";

import { Field } from "../../field";
import { AgentName } from "../../agent_names";
import { mcpToolSchema, type McpToolArgumentsFor } from "../../mcp_tool_schema";
import { definePreview, type ToolPreview } from "../entry";
import { GRANTS_SERVER_ID } from "../server_ids";
import {
  COMPACT_ITEM_LIMIT,
  MoreLine,
  plural,
  PreviewBadge,
  PreviewText,
  PreviewTitle,
  type PreviewProps,
} from "../vocabulary";

export const zCreateGrantArgs: z.ZodType<McpToolArgumentsFor<typeof GRANTS_SERVER_ID, "create_grant">> = mcpToolSchema(
  GRANTS_SERVER_ID,
  "create_grant"
);
const zRevokeGrantsArgs: z.ZodType<McpToolArgumentsFor<typeof GRANTS_SERVER_ID, "revoke_grants">> = mcpToolSchema(
  GRANTS_SERVER_ID,
  "revoke_grants"
);

type CreateGrantArgs = z.infer<typeof zCreateGrantArgs>;
type RevokeGrantsArgs = z.infer<typeof zRevokeGrantsArgs>;
type CreateGrantItem = CreateGrantArgs["grants"][number];

// Loose structural shapes the widgets render, so both the argument catalog (min-length arrays
// generate as non-empty tuples) and the result catalog (plain arrays) satisfy them — the generated
// per-catalog types diverge there, and one grant renders identically from either side. This mirrors
// the pre-#4918 approach; the shared `domain` tag routes each item to the right shape.
type KubernetesRule = {
  api_groups?: readonly string[];
  resources?: readonly string[];
  verbs: readonly string[];
  resource_names?: readonly string[];
  non_resource_urls?: readonly string[];
};
type KubernetesGrantScope =
  | { kind: "namespaces"; namespaces: readonly string[] }
  | { kind: "all_namespaces" }
  | { kind: "cluster" }
  | { kind: "non_resource" };
export type KubernetesGrantShape = { scope: KubernetesGrantScope; rules: readonly KubernetesRule[] };
export type HttpGrantShape = {
  origin: { scheme: string; host: string; port: number };
  coverage: { methods: readonly string[]; path_regex?: string | null };
  credential_handle?: string | null;
};

export function scopeLabel(scope: KubernetesGrantScope): string {
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

function ruleTarget(rule: KubernetesRule): string {
  if (rule.non_resource_urls?.length) return rule.non_resource_urls.join(", ");
  const group = rule.api_groups?.filter(Boolean).join(", ");
  const resources = rule.resources?.join(", ") || "resources";
  const target = group ? `${group}/${resources}` : resources;
  return rule.resource_names?.length ? `${target} (${rule.resource_names.join(", ")})` : target;
}

function RuleLine({ rule }: { rule: KubernetesRule }) {
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

export function KubernetesGrantScopeAndRules({
  spec,
  variant,
}: {
  spec: KubernetesGrantShape;
  variant: "compact" | "detailed";
}): JSX.Element {
  const rules = variant === "compact" ? spec.rules.slice(0, 1) : spec.rules;
  return (
    <Stack gap={4}>
      <PreviewTitle>{scopeLabel(spec.scope)}</PreviewTitle>
      <Stack gap={2}>
        {rules.map((rule, index) => (
          <RuleLine key={index} rule={rule} />
        ))}
        <MoreLine count={spec.rules.length - rules.length} />
      </Stack>
    </Stack>
  );
}

export function httpOriginLabel(origin: HttpGrantShape["origin"]): string {
  return `${origin.scheme}://${origin.host}:${origin.port}`;
}

export function HttpGrantCoverage({ spec }: { spec: HttpGrantShape }): JSX.Element {
  return (
    <Stack gap={4}>
      <Group gap={6}>
        <PreviewTitle className="haku-shell-mono">{httpOriginLabel(spec.origin)}</PreviewTitle>
        <PreviewBadge variant="light" color="teal">
          {spec.coverage.methods.join(", ")}
        </PreviewBadge>
      </Group>
      {spec.coverage.path_regex && (
        <PreviewText span className="haku-shell-mono">
          {spec.coverage.path_regex}
        </PreviewText>
      )}
      {spec.credential_handle && <Field label="Credential">{spec.credential_handle}</Field>}
    </Stack>
  );
}

function GrantItemView({ item, variant }: { item: CreateGrantItem; variant: "compact" | "detailed" }): JSX.Element {
  if (item.domain === "kubernetes") return <KubernetesGrantScopeAndRules spec={item.spec} variant={variant} />;
  return <HttpGrantCoverage spec={item.spec} />;
}

function domainLabel(item: CreateGrantItem): string {
  return item.domain === "kubernetes" ? "Kubernetes" : "HTTP egress";
}

function formatDuration(seconds: number): string {
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds % 60 === 0) return `${seconds / 60}m`;
  return `${seconds}s`;
}

function CreateGrantPreview({ args, variant }: PreviewProps<CreateGrantArgs>) {
  const grants = variant === "compact" ? args.grants.slice(0, COMPACT_ITEM_LIMIT) : args.grants;
  const domain = args.grants.length > 0 ? domainLabel(args.grants[0]) : "";
  return (
    <Stack gap="xs">
      <Group gap={6}>
        <PreviewTitle>
          {domain} {plural(args.grants.length, "grant")}
        </PreviewTitle>
        <PreviewBadge variant="outline">for {formatDuration(args.duration_seconds)}</PreviewBadge>
        <PreviewBadge variant="light">
          applies to {args.applies_to === "session" ? "this session" : "the Agent"}
        </PreviewBadge>
      </Group>
      <Stack gap="xs">
        {grants.map((item, index) => (
          <GrantItemView key={index} item={item} variant={variant} />
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

function RevokeGrantsPreview({ args, variant }: PreviewProps<RevokeGrantsArgs>) {
  return (
    <Stack gap="xs">
      <Group gap={6}>
        <PreviewTitle>{plural(args.grant_ids.length, "grant")}</PreviewTitle>
        <PreviewBadge variant="light">{args.domain}</PreviewBadge>
        <PreviewBadge variant="outline">{args.owner_agent_id ? "revoke" : "release"}</PreviewBadge>
      </Group>
      <GrantIdList ids={args.grant_ids} variant={variant} />
      {args.owner_agent_id && (
        <Field label="Owner agent">
          <AgentName agentId={args.owner_agent_id} />
        </Field>
      )}
      <Field label="Reason">{args.reason ?? "released"}</Field>
    </Stack>
  );
}

export const grantsPreviews: {
  create_grant: ToolPreview<typeof zCreateGrantArgs>;
  revoke_grants: ToolPreview<typeof zRevokeGrantsArgs>;
} = {
  create_grant: definePreview(zCreateGrantArgs, CreateGrantPreview),
  revoke_grants: definePreview(zRevokeGrantsArgs, RevokeGrantsPreview),
} satisfies Record<string, ToolPreview>;
