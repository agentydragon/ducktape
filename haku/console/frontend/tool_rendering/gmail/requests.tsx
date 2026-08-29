// Per-tool-type rendering for haku-console's in-process `gmail` MCP server (see
// haku/console/tools/gmail.py). Anything not shaped as expected falls back to the generic raw-JSON
// view (approval_state.ts's argumentsJson): arguments are validated by the tool's own Pydantic model
// at execution time, not at submission, so a pending approval's may be malformed. The remaining read
// tools (`labels_list`, `filters_list`, `drafts_list`, …) have no widget — their args are empty or
// self-descriptive. The Zod schemas below are built from the FastMCP input schemas advertised by
// tools/list; execution still owns cross-field rules JSON Schema cannot express, such as add/remove
// label overlap.

import { Group, Loader, Stack } from "@mantine/core";
import { useEffect, useState } from "react";
import type { z } from "zod";

import { CodeBlock } from "../../code_block";
import { Field } from "../../field";
import {
  fetchGmailMessagePreview,
  fetchGmailThreadPreviews,
  type GmailMessagePreview,
  type GmailThreadPreview,
} from "../../gmail_client";
import { MailIcon } from "../../icons";
import { ExternalLink } from "../../link";
import { mcpToolSchema, type McpToolArgumentsFor } from "../../mcp_tool_schema";
import { definePreview, type ToolPreview } from "../entry";
import { GMAIL_SERVER_ID } from "../server_ids";
import {
  COMPACT_ITEM_LIMIT,
  firstLines,
  MoreLine,
  PreviewBadge,
  PreviewText,
  PreviewTitle,
  type PreviewProps,
} from "../vocabulary";

const zModifyGmailThreadLabelsArgs: z.ZodType<McpToolArgumentsFor<typeof GMAIL_SERVER_ID, "threads_modify_labels">> =
  mcpToolSchema(GMAIL_SERVER_ID, "threads_modify_labels");
export const zCreateGmailDraftArgs: z.ZodType<McpToolArgumentsFor<typeof GMAIL_SERVER_ID, "drafts_create">> =
  mcpToolSchema(GMAIL_SERVER_ID, "drafts_create");
const zGetGmailThreadArgs: z.ZodType<McpToolArgumentsFor<typeof GMAIL_SERVER_ID, "threads_get">> = mcpToolSchema(
  GMAIL_SERVER_ID,
  "threads_get"
);
const zSearchGmailThreadsArgs: z.ZodType<McpToolArgumentsFor<typeof GMAIL_SERVER_ID, "threads_list">> = mcpToolSchema(
  GMAIL_SERVER_ID,
  "threads_list"
);
const zGetGmailMessageArgs: z.ZodType<McpToolArgumentsFor<typeof GMAIL_SERVER_ID, "messages_get">> = mcpToolSchema(
  GMAIL_SERVER_ID,
  "messages_get"
);

type ModifyGmailThreadLabelsArgs = z.infer<typeof zModifyGmailThreadLabelsArgs>;
export type CreateGmailDraftArgs = z.infer<typeof zCreateGmailDraftArgs>;
type GetGmailThreadArgs = z.infer<typeof zGetGmailThreadArgs>;
type SearchGmailThreadsArgs = z.infer<typeof zSearchGmailThreadsArgs>;
type GetGmailMessageArgs = z.infer<typeof zGetGmailMessageArgs>;

// A Gmail API thread id resolves directly in the web UI's `#all/` view — the same link the
// backend builds for thread previews (haku/console/tools/gmail.py `_THREAD_URL`).
export function gmailThreadUrl(threadId: string): string {
  return `https://mail.google.com/mail/u/0/#all/${threadId}`;
}

function GmailThreadRow({
  threadId,
  preview,
  showLabels,
}: {
  threadId: string;
  preview: GmailThreadPreview | undefined;
  showLabels: boolean;
}) {
  if (!preview) {
    return <PreviewText c="dimmed">{threadId} (couldn&apos;t load preview)</PreviewText>;
  }
  return (
    <Stack gap={2}>
      <ExternalLink href={preview.gmail_url} size="sm">
        {preview.subject ?? "(no subject)"}
      </ExternalLink>
      {showLabels && preview.current_label_names.length > 0 && (
        <Group gap={4}>
          {preview.current_label_names.map((name) => (
            <PreviewBadge key={name} variant="outline" color="gray">
              {name}
            </PreviewBadge>
          ))}
        </Group>
      )}
    </Stack>
  );
}

function ThreadLabelChanges({ args }: { args: ModifyGmailThreadLabelsArgs }) {
  // One row of pills; each pill's sign + color says which way its label goes — green `+ added`,
  // red `− removed` — so no separate "Add"/"Remove" heading is needed.
  return (
    <Group gap={6} align="center">
      {args.add?.map((name) => (
        <PreviewBadge key={`+${name}`} variant="light" color="teal">
          + {name}
        </PreviewBadge>
      ))}
      {args.remove?.map((name) => (
        <PreviewBadge key={`-${name}`} variant="light" color="red">
          − {name}
        </PreviewBadge>
      ))}
    </Group>
  );
}

function ModifyGmailThreadLabelsPreview({ args, variant }: PreviewProps<ModifyGmailThreadLabelsArgs>) {
  const [previews, setPreviews] = useState<Record<string, GmailThreadPreview> | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Both variants fetch the thread subjects (and labels) — they're the important human-
  // readable bit; compact just shows fewer rows and drops the current-label pills.
  useEffect(() => {
    let alive = true;
    setPreviews(null);
    setError(null);
    fetchGmailThreadPreviews(args.thread_ids)
      .then((result) => {
        if (alive) setPreviews(result);
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
    // args.thread_ids is a fresh array reference per render of a JSON-derived value; the
    // approval itself is immutable once submitted, so join() is a stable enough dep key.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [args.thread_ids.join(",")]);

  const compact = variant === "compact";
  const shownIds = compact ? args.thread_ids.slice(0, COMPACT_ITEM_LIMIT) : args.thread_ids;
  return (
    <Stack gap="xs">
      <ThreadLabelChanges args={args} />
      {error ? (
        <PreviewText c="red">{error}</PreviewText>
      ) : previews === null ? (
        <Loader size="xs" />
      ) : (
        <Field icon={<MailIcon size={15} />} label={`${args.thread_ids.length} threads`}>
          <Stack gap={4}>
            {shownIds.map((threadId) => (
              <GmailThreadRow key={threadId} threadId={threadId} preview={previews[threadId]} showLabels={!compact} />
            ))}
            <MoreLine count={args.thread_ids.length - shownIds.length} />
          </Stack>
        </Field>
      )}
    </Stack>
  );
}

// Exported for gmail/responses.tsx's drafts_create finished view — it re-shows the same clamped
// body the pending preview did, since the operator still cares whether the sent text matches.
export function CompactBody({ body }: { body: string }): JSX.Element {
  const { text, truncated } = firstLines(body, 2);
  return (
    <PreviewText c="dimmed" style={{ whiteSpace: "pre-wrap" }}>
      {text}
      {truncated ? " …" : ""}
    </PreviewText>
  );
}

// Exported for gmail/calls.tsx's combined drafts_create widget, which renders this pre-execution
// and CreateGmailDraftResultView (responses.tsx) once the call has finished.
export function CreateGmailDraftPreview({ args, variant }: PreviewProps<CreateGmailDraftArgs>): JSX.Element {
  // Subject leads as the draft's title; recipients ride one mail-icon line (cc folded in when
  // detailed); the body follows unlabelled — clamped compact, full detailed. A reply draft links to
  // the thread it lands in rather than printing the raw thread id, whose value the href carries.
  const detailed = variant === "detailed";
  return (
    <Stack gap={6}>
      <PreviewTitle>{args.subject}</PreviewTitle>
      <Field icon={<MailIcon size={15} />} label="Recipients">
        {args.to.join(", ")}
        {detailed && args.cc && args.cc.length > 0 && (
          <PreviewText span c="dimmed">{` · cc ${args.cc.join(", ")}`}</PreviewText>
        )}
      </Field>
      {detailed ? <CodeBlock value={args.body} /> : <CompactBody body={args.body} />}
      {args.thread_id && (
        <ExternalLink href={gmailThreadUrl(args.thread_id)} size="xs">
          Reply in thread ↗
        </ExternalLink>
      )}
    </Stack>
  );
}

// A thread/message id is opaque and not user-readable, so threads_get and messages_get resolve the
// real subject by fetching it, the same way ModifyGmailThreadLabelsPreview does. The raw id still
// rides along dimmed in detailed.
function GetGmailThreadPreview({ args, variant }: PreviewProps<GetGmailThreadArgs>) {
  const [preview, setPreview] = useState<GmailThreadPreview | null | undefined>(undefined);

  useEffect(() => {
    let alive = true;
    setPreview(undefined);
    fetchGmailThreadPreviews([args.id]).then((result) => {
      if (alive) setPreview(result[args.id] ?? null);
    });
    return () => {
      alive = false;
    };
  }, [args.id]);

  if (preview === undefined) return <Loader size="xs" />;
  if (preview === null) {
    return <PreviewText c="dimmed">{args.id} (couldn&apos;t load preview)</PreviewText>;
  }
  return (
    <Stack gap={2}>
      <ExternalLink href={preview.gmail_url} size="sm">
        {preview.subject ?? "(no subject)"}
      </ExternalLink>
      {variant === "detailed" && (
        <PreviewText size="xs" c="dimmed" className="haku-shell-mono">
          {args.id}
        </PreviewText>
      )}
    </Stack>
  );
}

function GetGmailMessagePreview({ args, variant }: PreviewProps<GetGmailMessageArgs>) {
  const [preview, setPreview] = useState<GmailMessagePreview | null | undefined>(undefined);

  useEffect(() => {
    let alive = true;
    setPreview(undefined);
    fetchGmailMessagePreview(args.id).then((result) => {
      if (alive) setPreview(result);
    });
    return () => {
      alive = false;
    };
  }, [args.id]);

  if (preview === undefined) return <Loader size="xs" />;
  if (preview === null) {
    return <PreviewText c="dimmed">{args.id} (couldn&apos;t load preview)</PreviewText>;
  }
  return (
    <Stack gap={2}>
      <ExternalLink href={preview.gmail_url} size="sm">
        {preview.subject ?? "(no subject)"}
      </ExternalLink>
      {variant === "detailed" && (
        <PreviewText size="xs" c="dimmed" className="haku-shell-mono">
          {args.id}
        </PreviewText>
      )}
    </Stack>
  );
}

function SearchGmailThreadsPreview({ args }: PreviewProps<SearchGmailThreadsArgs>) {
  return (
    <PreviewText>
      Search:{" "}
      <PreviewText span className="haku-shell-mono">
        {args.q}
      </PreviewText>
    </PreviewText>
  );
}

/** Per-tool preview widgets for the `gmail` server. `drafts_create` has no entry here — its
 * pending/finished states are one combined widget (calls.tsx), not a separate args-only one. The
 * remaining read tools (`labels_list`, `filters_list`, `drafts_list`, …) have no entry either —
 * their args are empty or self-descriptive, so the raw-JSON fallback serves. */
export const gmailPreviews: {
  threads_modify_labels: ToolPreview<typeof zModifyGmailThreadLabelsArgs>;
  threads_get: ToolPreview<typeof zGetGmailThreadArgs>;
  threads_list: ToolPreview<typeof zSearchGmailThreadsArgs>;
  messages_get: ToolPreview<typeof zGetGmailMessageArgs>;
} = {
  threads_modify_labels: definePreview(zModifyGmailThreadLabelsArgs, ModifyGmailThreadLabelsPreview),
  threads_get: definePreview(zGetGmailThreadArgs, GetGmailThreadPreview),
  threads_list: definePreview(zSearchGmailThreadsArgs, SearchGmailThreadsPreview),
  messages_get: definePreview(zGetGmailMessageArgs, GetGmailMessagePreview),
} satisfies Record<string, ToolPreview>;
