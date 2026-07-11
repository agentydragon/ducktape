// Per-tool-type rendering for haku-console's in-process `gmail` MCP server's write tools
// (see haku/console/tools/gmail.py). Falls back to the generic raw-JSON view
// (approval_state.ts's argumentsJson) for anything that isn't shaped as expected — arguments
// are only validated by the tool's own Pydantic model at execution time, not at submission,
// so a pending approval's arguments could in principle be malformed. The read tools have no
// widget here (their args — a query, an id, a format — are self-descriptive). The zod schemas
// below are generated from the write tools' Pydantic argument models (:schema_zod), so this
// file's shape checks can never drift from the backend's — see haku/console/tools/gmail.py.

import { Anchor, Badge, Group, Loader, Stack, Text } from "@mantine/core";
import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import type { z } from "zod";

import { zBatchModifyGmailThreadLabelsArgs, zCreateGmailDraftArgs } from "../api/schema.zod.ts";
import { Field } from "../field.tsx";
import { fetchGmailThreadPreviews, type GmailThreadPreview } from "../gmail_client.ts";
import { COMPACT_ITEM_LIMIT, firstLines, MoreLine, type PreviewVariant } from "./variant.tsx";

export const GMAIL_SERVER_ID = "gmail";

type BatchModifyGmailThreadLabelsArgs = z.infer<typeof zBatchModifyGmailThreadLabelsArgs>;
type CreateGmailDraftArgs = z.infer<typeof zCreateGmailDraftArgs>;

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
    <Stack gap={0}>
      <Anchor href={preview.gmail_url} target="_blank" rel="noreferrer" size="sm">
        {preview.subject ?? "(no subject)"}
      </Anchor>
      {showLabels && preview.current_label_names.length > 0 && (
        <Text size="xs" c="dimmed">
          Current labels: {preview.current_label_names.join(", ")}
        </Text>
      )}
    </Stack>
  );
}

function ThreadLabelChanges({ args }: { args: BatchModifyGmailThreadLabelsArgs }) {
  return (
    <>
      {(args.add?.length ?? 0) > 0 && (
        <Field label="Add labels">
          <Group gap={4}>
            {args.add?.map((name) => (
              <Badge key={name} variant="light" color="teal">
                {name}
              </Badge>
            ))}
          </Group>
        </Field>
      )}
      {(args.remove?.length ?? 0) > 0 && (
        <Field label="Remove labels">
          <Group gap={4}>
            {args.remove?.map((name) => (
              <Badge key={name} variant="light" color="red">
                {name}
              </Badge>
            ))}
          </Group>
        </Field>
      )}
    </>
  );
}

function BatchModifyGmailThreadLabelsPreview({
  args,
  variant,
}: {
  args: BatchModifyGmailThreadLabelsArgs;
  variant: PreviewVariant;
}) {
  const [previews, setPreviews] = useState<Record<string, GmailThreadPreview> | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Both variants fetch the thread subjects (and labels) — they're the important human-
  // readable bit; compact just shows fewer rows and drops the current-label sublines.
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
      <Field label={`Threads (${args.thread_ids.length})`}>
        {error ? (
          <Text size="sm" c="red">
            {error}
          </Text>
        ) : previews === null ? (
          <Loader size="xs" />
        ) : (
          <Stack gap="xs">
            {shownIds.map((threadId) => (
              <GmailThreadRow key={threadId} threadId={threadId} preview={previews[threadId]} showLabels={!compact} />
            ))}
            <MoreLine count={args.thread_ids.length - shownIds.length} />
          </Stack>
        )}
      </Field>
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

function CreateGmailDraftPreview({ args, variant }: { args: CreateGmailDraftArgs; variant: PreviewVariant }) {
  // Common trunk: To → Subject → Body; compact clamps the body and drops Cc/thread, detailed
  // shows the full body plus them.
  const detailed = variant === "detailed";
  return (
    <Stack gap="xs">
      <Field label="To">{args.to.join(", ")}</Field>
      {detailed && args.cc && args.cc.length > 0 && <Field label="Cc">{args.cc.join(", ")}</Field>}
      <Field label="Subject">{args.subject}</Field>
      <Field label="Body">
        {detailed ? <pre className="haku-shell-json">{args.body}</pre> : <CompactBody body={args.body} />}
      </Field>
      {detailed && args.thread_id && <Field label="Replying within thread">{args.thread_id}</Field>}
    </Stack>
  );
}

/** Nice per-tool rendering for the `gmail` server's write tools; `null` when the (tool,
 * arguments) pair doesn't match a known widget, so the caller falls back to raw JSON (as the
 * read tools, whose args are self-descriptive, always do). */
export function gmailToolPreview(
  toolName: string,
  args: Record<string, unknown>,
  variant: PreviewVariant
): ReactNode | null {
  if (toolName === "threads_batch_modify") {
    const parsed = zBatchModifyGmailThreadLabelsArgs.safeParse(args);
    return parsed.success ? <BatchModifyGmailThreadLabelsPreview args={parsed.data} variant={variant} /> : null;
  }
  if (toolName === "drafts_create") {
    const parsed = zCreateGmailDraftArgs.safeParse(args);
    return parsed.success ? <CreateGmailDraftPreview args={parsed.data} variant={variant} /> : null;
  }
  return null;
}
