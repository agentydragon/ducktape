// Per-tool-type rendering for haku-console's in-process `gmail` MCP server's write tools
// (see haku/console/tools/gmail.py). Falls back to the generic raw-JSON view
// (approval_state.ts's argumentsJson) for anything that isn't shaped as expected — arguments
// are only validated by the tool's own Pydantic model at execution time, not at submission,
// so a pending approval's arguments could in principle be malformed. The read tools have no
// widget here (their args — a query, an id, a format — are self-descriptive). The zod schemas
// below are generated from the write tools' Pydantic argument models (:schema_zod), so this
// file's shape checks can never drift from the backend's — see haku/console/tools/gmail.py.

import { Anchor, Badge, Group, Loader, Stack, Text } from "@mantine/core";
import { useEffect, useState } from "react";
import type { z } from "zod";

import { zBatchModifyGmailThreadLabelsArgs, zCreateGmailDraftArgs } from "../api/schema.zod.ts";
import { Field } from "../field.tsx";
import { fetchGmailThreadPreviews, type GmailThreadPreview } from "../gmail_client.ts";
import { MailIcon } from "../icons.tsx";
import { definePreview, type ToolPreview } from "./entry.tsx";
import { COMPACT_ITEM_LIMIT, firstLines, MoreLine, type PreviewProps } from "./variant.tsx";

export const GMAIL_SERVER_ID = "gmail";

type BatchModifyGmailThreadLabelsArgs = z.infer<typeof zBatchModifyGmailThreadLabelsArgs>;
type CreateGmailDraftArgs = z.infer<typeof zCreateGmailDraftArgs>;

// A Gmail API thread id resolves directly in the web UI's `#all/` view — the same link the
// backend builds for thread previews (haku/console/tools/gmail.py `_THREAD_URL`).
function gmailThreadUrl(threadId: string): string {
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
    return (
      <Text size="sm" c="dimmed">
        {threadId} (couldn't load preview)
      </Text>
    );
  }
  return (
    <Stack gap={2}>
      <Anchor href={preview.gmail_url} target="_blank" rel="noreferrer" size="sm">
        {preview.subject ?? "(no subject)"}
      </Anchor>
      {showLabels && preview.current_label_names.length > 0 && (
        <Group gap={4}>
          {preview.current_label_names.map((name) => (
            <Badge key={name} variant="outline" color="gray" size="sm">
              {name}
            </Badge>
          ))}
        </Group>
      )}
    </Stack>
  );
}

function ThreadLabelChanges({ args }: { args: BatchModifyGmailThreadLabelsArgs }) {
  // One row of pills; each pill's sign + color says which way its label goes — green `+ added`,
  // red `− removed` — so no separate "Add"/"Remove" heading is needed.
  return (
    <Group gap={6} align="center">
      {args.add?.map((name) => (
        <Badge key={`+${name}`} variant="light" color="teal">
          + {name}
        </Badge>
      ))}
      {args.remove?.map((name) => (
        <Badge key={`-${name}`} variant="light" color="red">
          − {name}
        </Badge>
      ))}
    </Group>
  );
}

function BatchModifyGmailThreadLabelsPreview({ args, variant }: PreviewProps<BatchModifyGmailThreadLabelsArgs>) {
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
        <Text size="sm" c="red">
          {error}
        </Text>
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

function CompactBody({ body }: { body: string }) {
  const { text, truncated } = firstLines(body, 2);
  return (
    <Text size="sm" c="dimmed" style={{ whiteSpace: "pre-wrap" }}>
      {text}
      {truncated ? " …" : ""}
    </Text>
  );
}

function CreateGmailDraftPreview({ args, variant }: PreviewProps<CreateGmailDraftArgs>) {
  // Subject leads as the draft's title; recipients ride one mail-icon line (cc folded in when
  // detailed); the body follows unlabelled — clamped compact, full detailed. A reply draft
  // links to the thread it lands in (useful in both variants) rather than printing the raw
  // thread id, which is noise — the link's href carries the id for anyone who needs it.
  const detailed = variant === "detailed";
  return (
    <Stack gap={6}>
      <Text fw={600}>{args.subject}</Text>
      <Field icon={<MailIcon size={15} />} label="Recipients">
        {args.to.join(", ")}
        {detailed && args.cc && args.cc.length > 0 && <Text span c="dimmed">{` · cc ${args.cc.join(", ")}`}</Text>}
      </Field>
      {detailed ? <pre className="haku-shell-json">{args.body}</pre> : <CompactBody body={args.body} />}
      {args.thread_id && (
        <Anchor href={gmailThreadUrl(args.thread_id)} target="_blank" rel="noreferrer" size="xs">
          Reply in thread ↗
        </Anchor>
      )}
    </Stack>
  );
}

/** Per-tool preview widgets for the `gmail` server's write tools. Read tools have no entry —
 * their args (a query, an id, a format) are self-descriptive, so the raw-JSON fallback serves. */
export const gmailPreviews = {
  threads_batch_modify: definePreview(zBatchModifyGmailThreadLabelsArgs, BatchModifyGmailThreadLabelsPreview),
  drafts_create: definePreview(zCreateGmailDraftArgs, CreateGmailDraftPreview),
} satisfies Record<string, ToolPreview>;
