// Result rendering for the in-process `gmail` server (the argument-side widgets live in
// ./requests.tsx). Every Zod schema below is the FastMCP-advertised output schema for its tool,
// generated in mcp_tool_result_schema.ts from tools/list: the Gmail API `Thread` and `Message`
// resource shapes (camelCase wire aliases) verbatim.

import { Group, Loader, Stack } from "@mantine/core";
import { type ReactNode, useEffect, useState } from "react";
import type { z } from "zod";

import { Field } from "../../field";
import { fetchGmailLabelNames, messageSubject } from "../../gmail_client";
import { GmailIcon, MailIcon } from "../../icons";
import { ExternalLink } from "../../link";
import { mcpToolResultSchema, type McpToolResultFor } from "../../mcp_tool_result_schema";
import { defineResultPreview, type ResultPreviewProps, type ToolResultPreview } from "../result_entry";
import { GMAIL_SERVER_ID } from "../server_ids";
import { firstLines, plural, PreviewBadge, PreviewText } from "../vocabulary";
import { gmailThreadUrl } from "./requests";

const zThread: z.ZodType<McpToolResultFor<typeof GMAIL_SERVER_ID, "threads_get">> = mcpToolResultSchema(
  GMAIL_SERVER_ID,
  "threads_get"
);
const zMessage: z.ZodType<McpToolResultFor<typeof GMAIL_SERVER_ID, "messages_get">> = mcpToolResultSchema(
  GMAIL_SERVER_ID,
  "messages_get"
);

type GmailThread = z.infer<typeof zThread>;
type GmailMessage = z.infer<typeof zMessage>;

// A message/thread's `labelIds` are opaque ids; resolve display names via the read-only
// `labels_list` tool, same composition `gmail_client.ts`'s `fetchGmailThreadPreviews` uses for
// the `threads_modify_labels` preview. Fetched once per rendered widget; while loading (or on
// failure) label pills fall back to the raw id.
function useGmailLabelNames(): ReadonlyMap<string, string> | null {
  const [names, setNames] = useState<ReadonlyMap<string, string> | null>(null);

  useEffect(() => {
    let alive = true;
    fetchGmailLabelNames()
      .then((result) => {
        if (alive) setNames(result);
      })
      .catch((error: unknown) => {
        console.warn("Could not resolve Gmail label names", error);
      });
    return () => {
      alive = false;
    };
  }, []);

  return names;
}

function LabelPills({ labelIds, names }: { labelIds: string[]; names: ReadonlyMap<string, string> | null }) {
  if (labelIds.length === 0) return null;
  if (!names) return <Loader size="xs" />;
  return (
    <Group gap={4}>
      {labelIds.map((id) => (
        <PreviewBadge key={id} variant="outline" color="gray">
          {names.get(id) ?? id}
        </PreviewBadge>
      ))}
    </Group>
  );
}

// Opens Gmail in a new tab, with Gmail's own icon marking it external. Shared by GmailLink below.
function GmailIconLink({ href, fw, children }: { href: string; fw?: number; children: ReactNode }) {
  return (
    <ExternalLink href={href} size="sm" fw={fw}>
      <Group gap={4} wrap="nowrap" align="center">
        <GmailIcon size={15} />
        <span>{children}</span>
      </Group>
    </ExternalLink>
  );
}

// `gmailThreadUrl`'s `#all/<id>` fragment resolves either a thread or a message id, so callers pass
// whichever id they have. Used for a thread's/message's subject (bold, the card's identity).
function GmailLink({ id, fw, children }: { id: string; fw?: number; children: ReactNode }) {
  return (
    <GmailIconLink href={gmailThreadUrl(id)} fw={fw}>
      {children}
    </GmailIconLink>
  );
}

function GmailThreadResultView({ result, variant }: ResultPreviewProps<GmailThread>) {
  const names = useGmailLabelNames();
  const detailed = variant === "detailed";
  const firstMessage = result.messages?.[0];
  const snippet = result.snippet ?? firstMessage?.snippet ?? "";
  const body = detailed ? snippet : firstLines(snippet, 2).text;
  return (
    <Stack gap={6}>
      <GmailLink id={result.id ?? ""} fw={600}>
        {messageSubject(firstMessage) ?? "(no subject)"}
      </GmailLink>
      {body && (
        <PreviewText c="dimmed" style={{ whiteSpace: "pre-wrap" }}>
          {body}
        </PreviewText>
      )}
      <Field icon={<MailIcon size={15} />} label={plural(result.messages?.length ?? 0, "message")}>
        {detailed && <LabelPills labelIds={firstMessage?.labelIds ?? []} names={names} />}
      </Field>
    </Stack>
  );
}

function GmailMessageResultView({ result, variant }: ResultPreviewProps<GmailMessage>) {
  const names = useGmailLabelNames();
  const detailed = variant === "detailed";
  const snippet = result.snippet ?? "";
  const body = detailed ? snippet : firstLines(snippet, 2).text;
  return (
    <Stack gap={6}>
      <GmailLink id={result.threadId ?? result.id ?? ""} fw={600}>
        {messageSubject(result) ?? "(no subject)"}
      </GmailLink>
      {body && (
        <PreviewText c="dimmed" style={{ whiteSpace: "pre-wrap" }}>
          {body}
        </PreviewText>
      )}
      {detailed && <LabelPills labelIds={result.labelIds ?? []} names={names} />}
    </Stack>
  );
}

/** Per-tool result widgets for the `gmail` server. */
export const gmailResultPreviews: {
  threads_get: ToolResultPreview<typeof zThread>;
  messages_get: ToolResultPreview<typeof zMessage>;
} = {
  threads_get: defineResultPreview(zThread, GmailThreadResultView),
  messages_get: defineResultPreview(zMessage, GmailMessageResultView),
} satisfies Record<string, ToolResultPreview>;
