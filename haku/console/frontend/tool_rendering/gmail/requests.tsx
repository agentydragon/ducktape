// Per-tool-type rendering for the `gmail` MCP server (see
// x/google_mcp_server/gmail.py). Anything not shaped as expected falls back to the generic raw-JSON
// view (tool_call_state.ts's argumentsJson): arguments are validated by the tool's own Pydantic model
// at execution time, not at submission, so a pending approval's may be malformed. The remaining read
// tools (`labels_list`, `filters_list`, `drafts_list`, …) have no widget — their args are empty or
// self-descriptive. The Zod schemas below are built from the FastMCP input schemas advertised by
// tools/list; execution still owns cross-field rules JSON Schema cannot express.

import { Loader, Stack } from "@mantine/core";
import { useEffect, useState } from "react";
import type { z } from "zod";

import { fetchGmailMessagePreview, fetchGmailThreadPreview, type GmailLinkPreview } from "../../gmail_client";
import { ExternalLink } from "../../link";
import { mcpToolSchema, type McpToolArgumentsFor } from "../../mcp_tool_schema";
import { definePreview, type ToolPreview } from "../entry";
import { GMAIL_SERVER_ID } from "../server_ids";
import { PreviewText, type PreviewProps } from "../vocabulary";

const zGetGmailThreadArgs: z.ZodType<McpToolArgumentsFor<typeof GMAIL_SERVER_ID, "threads_get">> = mcpToolSchema(
  GMAIL_SERVER_ID,
  "threads_get"
);
const zGetGmailMessageArgs: z.ZodType<McpToolArgumentsFor<typeof GMAIL_SERVER_ID, "messages_get">> = mcpToolSchema(
  GMAIL_SERVER_ID,
  "messages_get"
);

type GetGmailThreadArgs = z.infer<typeof zGetGmailThreadArgs>;
type GetGmailMessageArgs = z.infer<typeof zGetGmailMessageArgs>;

function GmailIdentityPreview({
  id,
  fetchPreview,
  kind,
  variant,
}: {
  id: string;
  fetchPreview: (id: string) => Promise<GmailLinkPreview | null>;
  kind: "thread" | "message";
  variant: "compact" | "detailed";
}) {
  const [preview, setPreview] = useState<GmailLinkPreview | null | undefined>(undefined);

  useEffect(() => {
    let alive = true;
    setPreview(undefined);
    fetchPreview(id).then(
      (result) => {
        if (alive) setPreview(result);
      },
      (error: unknown) => {
        console.warn(`Could not resolve Gmail ${kind} preview ${id}`, error);
        if (alive) setPreview(null);
      }
    );
    return () => {
      alive = false;
    };
  }, [id, fetchPreview, kind]);

  if (preview === undefined) return <Loader size="xs" />;
  if (preview === null) {
    return <PreviewText c="dimmed">{id} (couldn&apos;t load preview)</PreviewText>;
  }
  return (
    <Stack gap={2}>
      <ExternalLink href={preview.gmail_url} size="sm">
        {preview.subject ?? "(no subject)"}
      </ExternalLink>
      {variant === "detailed" && (
        <PreviewText size="xs" c="dimmed" className="haku-shell-mono">
          {id}
        </PreviewText>
      )}
    </Stack>
  );
}

function GetGmailThreadPreview({ args, variant }: PreviewProps<GetGmailThreadArgs>) {
  return <GmailIdentityPreview id={args.id} fetchPreview={fetchGmailThreadPreview} kind="thread" variant={variant} />;
}

function GetGmailMessagePreview({ args, variant }: PreviewProps<GetGmailMessageArgs>) {
  return <GmailIdentityPreview id={args.id} fetchPreview={fetchGmailMessagePreview} kind="message" variant={variant} />;
}

/** Per-tool preview widgets for the `gmail` server. */
export const gmailPreviews: {
  threads_get: ToolPreview<typeof zGetGmailThreadArgs>;
  messages_get: ToolPreview<typeof zGetGmailMessageArgs>;
} = {
  threads_get: definePreview(zGetGmailThreadArgs, GetGmailThreadPreview),
  messages_get: definePreview(zGetGmailMessageArgs, GetGmailMessagePreview),
} satisfies Record<string, ToolPreview>;
