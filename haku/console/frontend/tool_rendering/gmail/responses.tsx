// Result rendering for the in-process `gmail` server (the argument-side widgets live in
// ./requests.tsx). Every Zod schema below is the FastMCP-advertised output schema for its tool,
// generated in mcp_tool_result_schema.ts from tools/list: the Gmail API resource shapes
// (gmail_api/messages.py's `Draft`/`Thread`/`Message`/`ThreadsListResponse`, camelCase wire
// aliases) verbatim.

import { Group, Loader, Stack } from "@mantine/core";
import { type ReactNode, useEffect, useState } from "react";
import type { z } from "zod";

import { Field } from "../../field.tsx";
import { fetchGmailLabelNames, messageSubject } from "../../gmail_client.ts";
import { GmailIcon, MailIcon } from "../../icons.tsx";
import { ExternalLink } from "../../link.tsx";
import { mcpToolResultSchema } from "../../mcp_tool_result_schema.ts";
import { defineResultPreview, type ResultPreviewProps, type ToolResultPreview } from "../result_entry.tsx";
import { COMPACT_ITEM_LIMIT, firstLines, MoreLine, plural, PreviewBadge, PreviewText } from "../vocabulary.tsx";
import { GMAIL_SERVER_ID, gmailThreadUrl } from "./requests.tsx";

// Gmail's compose view opens a draft directly by its id.
function gmailDraftUrl(draftId: string): string {
  return `https://mail.google.com/mail/u/0/#drafts?compose=${draftId}`;
}

const zDraft = mcpToolResultSchema(GMAIL_SERVER_ID, "drafts_create");
const zThread = mcpToolResultSchema(GMAIL_SERVER_ID, "threads_get");
const zThreadsList = mcpToolResultSchema(GMAIL_SERVER_ID, "threads_list");
const zMessage = mcpToolResultSchema(GMAIL_SERVER_ID, "messages_get");

type Draft = z.infer<typeof zDraft>;
type GmailThread = z.infer<typeof zThread>;
type GmailThreadsList = z.infer<typeof zThreadsList>;
type GmailMessage = z.infer<typeof zMessage>;

// The draft's own subject is the identity, like every other Gmail link here — not generic "Open
// draft in Gmail ↗" text. The raw draft id is provenance, so it rides along dimmed only in
// detailed (the link's href carries it for anyone who needs it compact).
function CreateGmailDraftResultView({ result, variant }: ResultPreviewProps<Draft>) {
  return (
    <Group gap={6}>
      <GmailIconLink href={gmailDraftUrl(result.id)} fw={600}>
        {messageSubject(result.message) ?? "(no subject)"}
      </GmailIconLink>
      {variant === "detailed" && (
        <PreviewText span size="xs" c="dimmed">
          draft {result.id}
        </PreviewText>
      )}
    </Group>
  );
}

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

// Opens Gmail in a new tab, Gmail's own icon marking it external — the Gmail counterpart of
// google_calendar/responses.tsx's `EventTitle`. Shared by GmailLink below (thread/message ids,
// all opened via the same `#all/<id>` URL) and the draft link above (a different URL scheme, so
// it composes its own href instead of taking an id).
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

// `gmailThreadUrl`'s `#all/<id>` fragment resolves either a thread or a message id, so callers
// just pass whichever id they have instead of composing the URL themselves. Reused for a
// thread's/message's subject (bold, the card's identity) and a thread-list row's snippet (plain
// weight).
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
      <GmailLink id={result.id} fw={600}>
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

function GmailThreadsListResultView({ result, variant }: ResultPreviewProps<GmailThreadsList>) {
  const threads = result.threads ?? [];
  const shown = variant === "compact" ? threads.slice(0, COMPACT_ITEM_LIMIT) : threads;
  return (
    <Stack gap="xs">
      {shown.length > 0 ? (
        <Stack gap={4}>
          {shown.map((thread) => (
            <GmailLink key={thread.id} id={thread.id}>
              {thread.snippet ? firstLines(thread.snippet, 1).text : thread.id}
            </GmailLink>
          ))}
          <MoreLine count={threads.length - shown.length} />
        </Stack>
      ) : (
        <PreviewText c="dimmed">No threads found</PreviewText>
      )}
      {result.nextPageToken && variant === "detailed" && (
        <PreviewText size="xs" c="dimmed">
          More threads available
        </PreviewText>
      )}
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
      <GmailLink id={result.threadId ?? result.id} fw={600}>
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
export const gmailResultPreviews = {
  drafts_create: defineResultPreview(zDraft, CreateGmailDraftResultView),
  threads_get: defineResultPreview(zThread, GmailThreadResultView),
  threads_list: defineResultPreview(zThreadsList, GmailThreadsListResultView),
  messages_get: defineResultPreview(zMessage, GmailMessageResultView),
} satisfies Record<string, ToolResultPreview>;
