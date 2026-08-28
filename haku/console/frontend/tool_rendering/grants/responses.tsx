import { Group, Stack } from "@mantine/core";
import type { z } from "zod";

import { formatTimestamp } from "../../approval_state";
import { Field } from "../../field";
import { mcpToolResultSchema, type McpToolResultFor } from "../../mcp_tool_result_schema";
import { defineResultPreview, type ResultPreviewProps, type ToolResultPreview } from "../result_entry";
import { GRANTS_SERVER_ID } from "../server_ids";
import {
  COMPACT_ITEM_LIMIT,
  MoreLine,
  plural,
  PreviewBadge,
  PreviewText,
  PreviewTitle,
  type PreviewVariant,
} from "../vocabulary";
import { HttpGrantCoverage, KubernetesGrantScopeAndRules } from "./requests";

const zCreateGrantResult: z.ZodType<McpToolResultFor<typeof GRANTS_SERVER_ID, "create_grant">> = mcpToolResultSchema(
  GRANTS_SERVER_ID,
  "create_grant"
);
const zReleaseGrantsResult: z.ZodType<McpToolResultFor<typeof GRANTS_SERVER_ID, "release_grants">> =
  mcpToolResultSchema(GRANTS_SERVER_ID, "release_grants");

type GrantView = McpToolResultFor<typeof GRANTS_SERVER_ID, "release_grants">[number];
type GrantPrincipal = GrantView["grant"]["principal"];

function statusColor(status: string): string {
  switch (status) {
    case "active":
      return "teal";
    case "released":
      return "blue";
    case "revoked":
      return "red";
    default:
      return "gray";
  }
}

function Timestamp({ label, value }: { label: string; value: string }) {
  const timestamp = formatTimestamp(value);
  return (
    <Field label={label}>
      <span title={timestamp.title}>{timestamp.text}</span>
    </Field>
  );
}

export function principalText(principal: GrantPrincipal): string {
  switch (principal.kind) {
    case "agent":
      return `Agent ${principal.agent_id}`;
    case "session":
      return `Session ${principal.session_id}`;
  }
}

function GrantCoverage({ view, variant }: { view: GrantView; variant: PreviewVariant }): JSX.Element {
  if (view.domain === "kubernetes") {
    return (
      <KubernetesGrantScopeAndRules spec={{ scope: view.grant.scope, rules: view.grant.rules }} variant={variant} />
    );
  }
  return <HttpGrantCoverage spec={view.grant.spec} />;
}

function GrantResult({ view, variant }: { view: GrantView; variant: PreviewVariant }) {
  const grant = view.grant;
  const endedAt = grant.released_at ?? grant.revoked_at;
  return (
    <Stack gap="xs">
      <Group gap={6}>
        <PreviewTitle className="haku-shell-mono">{grant.grant_id}</PreviewTitle>
        <PreviewBadge variant="light" color={statusColor(grant.status)}>
          {grant.status}
        </PreviewBadge>
        <PreviewBadge variant="outline">{view.domain}</PreviewBadge>
      </Group>
      <GrantCoverage view={view} variant={variant} />
      {variant === "detailed" && (
        <Stack gap={2}>
          <Field label="Applies to">{principalText(grant.principal)}</Field>
          <Timestamp label="Created" value={grant.created_at} />
          <Timestamp label="Expires" value={grant.expires_at} />
          {endedAt && <Timestamp label="Ended" value={endedAt} />}
          {grant.end_reason && <Field label="End reason">{grant.end_reason}</Field>}
        </Stack>
      )}
    </Stack>
  );
}

function GrantsResult({ grants, variant }: { grants: GrantView[]; variant: PreviewVariant }) {
  const shown = variant === "compact" ? grants.slice(0, COMPACT_ITEM_LIMIT) : grants;
  return (
    <Stack gap="xs">
      <PreviewText c="dimmed">{plural(grants.length, "grant")} returned</PreviewText>
      {shown.map((view) => (
        <GrantResult key={view.grant.grant_id} view={view} variant={variant} />
      ))}
      <MoreLine count={grants.length - shown.length} />
    </Stack>
  );
}

function CreateGrantResult({ result, variant }: ResultPreviewProps<z.infer<typeof zCreateGrantResult>>) {
  return <GrantsResult grants={result} variant={variant} />;
}

function ReleaseGrantsResult({ result, variant }: ResultPreviewProps<z.infer<typeof zReleaseGrantsResult>>) {
  return <GrantsResult grants={result} variant={variant} />;
}

export const grantsResultPreviews: {
  create_grant: ToolResultPreview<typeof zCreateGrantResult>;
  release_grants: ToolResultPreview<typeof zReleaseGrantsResult>;
} = {
  create_grant: defineResultPreview(zCreateGrantResult, CreateGrantResult),
  release_grants: defineResultPreview(zReleaseGrantsResult, ReleaseGrantsResult),
} satisfies Record<string, ToolResultPreview>;
