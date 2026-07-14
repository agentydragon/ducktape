// The standalone preview card each per-server screenshot target renders. Shared across servers;
// each server's `preview_harness.tsx` imports `mountPreviewCards` (see mount.tsx) with its own
// fixtures. render.mjs (in this dir) drives one page load per fixture × variant × color scheme
// and element-screenshots `.haku-preview-card`.
import type { ReactNode } from "react";

import { approvalDisplayFields } from "../../approval_state.ts";
import type { ToolCallRecord } from "../../client.ts";
import { ToolCallCard } from "../../tool_call_card.tsx";
import type { PreviewVariant } from "../vocabulary.tsx";

export type StoredToolResult = NonNullable<ToolCallRecord["result"]>;

export type PreviewFixture = {
  title: string;
  serverId: string;
  toolName: string;
  args: Record<string, unknown>;
  // The tool's raw return value for a finished call; absent = the call renders as pending. The
  // per-server fixture types this against its result widget's schema (RegisteredToolPreviewFixture);
  // the harness widens it here and wraps it into the stored envelope at render time.
  result?: unknown;
};

// The stored wire shape of an executed call's result (mcp_approval.py's `_mcp_result_to_json`):
// FastMCP dumps the return into a JSON text block + structuredContent, wrapping a non-dict
// return (a list, a scalar) as `{"result": …}` with the wrap flagged in `_meta`. Shared so each
// server's preview fixtures build finished-call results the same way.
export function callToolResult(value: unknown): StoredToolResult {
  const wrap = typeof value !== "object" || value === null || Array.isArray(value);
  return {
    content: [{ type: "text", text: JSON.stringify(value) }],
    isError: false,
    structuredContent: wrap ? { result: value } : value,
    ...(wrap ? { _meta: { fastmcp: { wrap_result: true } } } : {}),
  };
}

// Slug for one preview fixture's PNG filename (mirrors target_slug in devinfra/ci/pr_visuals.py).
export function previewSlug(serverId: string, toolName: string): string {
  return `${serverId}-${toolName}`
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

// One stable slug + human label per fixture; duplicate (serverId, toolName) pairs get -2, -3, …
// so every preview PNG filename stays unique (writeVisualReviewManifest rejects duplicate paths).
export function previewFixtureSlugs(fixtures: PreviewFixture[]): { slug: string; label: string }[] {
  const seen = new Map<string, number>();
  return fixtures.map(({ serverId, toolName }) => {
    const base = previewSlug(serverId, toolName);
    const count = seen.get(base) ?? 0;
    seen.set(base, count + 1);
    return { slug: count === 0 ? base : `${base}-${count + 1}`, label: `${serverId} · ${toolName}` };
  });
}

const noop = () => {};

export function PreviewCard({ fixture, variant }: { fixture: PreviewFixture; variant: PreviewVariant }): ReactNode {
  const { title, serverId, toolName, args, result } = fixture;
  // A sample with a result renders as a finished OK call (so the result body shows); one without
  // stays pending, like the approvals panel's cards. The fixture carries the tool's raw return;
  // wrap it into the stored CallToolResult envelope the card renders.
  const finished = result != null;
  const storedResult = finished ? callToolResult(result) : null;
  const fields = approvalDisplayFields({
    tool_call_id: `preview_${serverId}.${toolName}`,
    server_id: serverId,
    tool_name: toolName,
    caller_principal: "haku-agent-api-token",
    status: finished ? "ok" : "pending_approval",
    created_at: "2026-07-11T12:00:00Z",
    updated_at: "2026-07-11T12:00:00Z",
    arguments: args,
    rationale: "Sample rationale for the operator.",
    title,
    result: storedResult,
    error: null,
    denial_reason: null,
  });
  return (
    // `.haku-page` (position overridden to static) mirrors the real surface so the card's
    // page-scoped CSS applies; the inner `.haku-preview-card` is render.mjs's screenshot target —
    // an opaque background keeps the standalone PNG self-contained, and no forced height means a
    // tight crop at the card's real rendered height.
    <div className="haku-page" style={{ position: "static" }}>
      <div className="haku-preview-card" style={{ background: "var(--haku-page-bg)", padding: 12, borderRadius: 8 }}>
        <ToolCallCard
          fields={fields}
          args={args}
          variant={variant}
          onVariantChange={noop}
          status={finished ? { label: "OK", color: "teal" } : { label: "Pending", color: "yellow" }}
          result={storedResult ?? undefined}
        />
      </div>
    </div>
  );
}
