// Result rendering for the in-process `gmail` server (the argument-side widgets live in
// ./requests.tsx). Every Zod schema below is the FastMCP-advertised output schema for its tool,
// generated in mcp_tool_result_schema.ts from tools/list: the Gmail API resource shapes
// (gmail_api/messages.py's `Draft`/`Thread`/`Message`/`ThreadsListResponse`, camelCase wire
// aliases) verbatim.

import { Group, Loader, Stack } from "@mantine/core";
import { type ReactNode, useEffect, useState } from "react";
import type { z } from "zod";

import { CodeBlock } from "../../code_block";
import { Field } from "../../field";
import { fetchGmailLabelNames, messageSubject } from "../../gmail_client";
import { GmailIcon, MailIcon } from "../../icons";
import { ExternalLink } from "../../link";
import { mcpToolResultSchema } from "../../mcp_tool_result_schema";
import { defineResultPreview, type ResultPreviewProps, type ToolResultPreview } from "../result_entry";
import {
  COMPACT_ITEM_LIMIT,
  firstLines,
  MoreLine,
  plural,
  PreviewBadge,
  PreviewText,
  type PreviewVariant,
} from "../vocabulary";
import { GMAIL_SERVER_ID } from "../server_ids";
import { CompactBody, gmailThreadUrl, type CreateGmailDraftArgs } from "./requests";

// Gmail's compose view opens a draft directly by its id.
function gmailDraftUrl(draftId: string): string {
  return `https://mail.google.com/mail/u/0/#drafts?compose=${draftId}`;
}

export const zDraft = mcpToolResultSchema(GMAIL_SERVER_ID, "drafts_create");
const zThread = mcpToolResultSchema(GMAIL_SERVER_ID, "threads_get");
const zThreadsList = mcpToolResultSchema(GMAIL_SERVER_ID, "threads_list");
const zMessage = mcpToolResultSchema(GMAIL_SERVER_ID, "messages_get");

export type Draft = z.infer<typeof zDraft>;
type GmailThread = z.infer<typeof zThread>;
type GmailThreadsList = z.infer<typeof zThreadsList>;
type GmailMessage = z.infer<typeof zMessage>;

// Exported for calls.tsx's combined drafts_create widget, rendered once the tool has executed. The
// draft's own subject is the identity, as with every other Gmail link here. Recipients and body
// still matter once the draft exists — the operator is verifying what actually got drafted — so
// they keep being shown from `args`. The raw draft id is provenance and rides along dimmed only in
// detailed; the link's href carries it either way.
export function CreateGmailDraftResultView({
  args,
  result,
  variant,
}: {
  args: CreateGmailDraftArgs;
  result: Draft;
  variant: PreviewVariant;
}) {
  const detailed = variant === "detailed";
  return (
    <Stack gap={6}>
      <GmailIconLink href={gmailDraftUrl(result.id ?? "")} fw={600}>
        {messageSubject(result.message) ?? args.subject}
      </GmailIconLink>
      <Field icon={<MailIcon size={15} />} label="Recipients">
        {args.to.join(", ")}
        {detailed && args.cc && args.cc.length > 0 && (
          <PreviewText span c="dimmed">{` · cc ${args.cc.join(", ")}`}</PreviewText>
        )}
      </Field>
      {detailed ? <CodeBlock value={args.body} /> : <CompactBody body={args.body} />}
      {detailed && (
        <PreviewText span size="xs" c="dimmed">
          draft {result.id}
        </PreviewText>
      )}
    </Stack>
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

// Opens Gmail in a new tab, Gmail's own icon marking it external. Shared by GmailLink below
// (thread/message ids, all opened via the same `#all/<id>` URL) and the draft link above, which
// composes its own href because its URL scheme differs.
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
// whichever id they have. Used for a thread's/message's subject (bold, the card's identity) and for
// a thread-list row's snippet (plain weight).
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

function GmailThreadsListResultView({ result, variant }: ResultPreviewProps<GmailThreadsList>) {
  const threads = result.threads ?? [];
  const shown = variant === "compact" ? threads.slice(0, COMPACT_ITEM_LIMIT) : threads;
  return (
    <Stack gap="xs">
      {shown.length > 0 ? (
        <Stack gap={4}>
          {shown.map((thread) => (
            <GmailLink key={thread.id ?? ""} id={thread.id ?? ""}>
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

/** Per-tool result widgets for the `gmail` server. `drafts_create` has no entry here — its
 * pending/finished states are one combined widget (calls.tsx), not a separate result-only one. */
export const gmailResultPreviews = {
  threads_get: defineResultPreview(zThread, GmailThreadResultView),
  threads_list: defineResultPreview(zThreadsList, GmailThreadsListResultView),
  messages_get: defineResultPreview(zMessage, GmailMessageResultView),
} satisfies Record<string, ToolResultPreview>;
