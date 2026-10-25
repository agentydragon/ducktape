import type { ActionPresentation } from "./presentation";
import { Anchor, Code, Group, Stack, Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import { JsonBlock, TextBlock } from "../../text_block";
import {
  defineCallPreview,
  definePreview,
  type ArgumentsPreview,
  type CallPreview,
  type CallPreviewProps,
} from "./entry";
import { defineResultPreview, type ResultPreview } from "./result_entry";

const draftArguments = z.strictObject({
  to: z.array(z.string()).min(1),
  subject: z.string(),
  body: z.string(),
  cc: z.array(z.string()).nullable().optional(),
  bcc: z.array(z.string()).nullable().optional(),
  thread_id: z.string().nullable().optional(),
});

type DraftArguments = z.infer<typeof draftArguments>;

export function offersCardDecisionButtonsDraftsCreate(args: unknown): boolean {
  return draftArguments.safeParse(args).success;
}

function recipients(args: DraftArguments): string {
  return args.to.join(", ");
}

function DraftLabel(): JSX.Element {
  return <Text fw={600}>Gmail: Draft email</Text>;
}

function DraftCollapsed({ args }: { args: DraftArguments }): JSX.Element {
  return (
    <Stack gap="xs">
      <Text size="sm" lineClamp={2} style={{ overflowWrap: "anywhere" }}>
        To {recipients(args)}
      </Text>
      {args.cc && args.cc.length > 0 && (
        <Text size="sm" lineClamp={2} style={{ overflowWrap: "anywhere" }}>
          Cc {args.cc.join(", ")}
        </Text>
      )}
      {args.bcc && args.bcc.length > 0 && (
        <Text size="sm" lineClamp={2} style={{ overflowWrap: "anywhere" }}>
          Bcc {args.bcc.join(", ")}
        </Text>
      )}
      <Text size="sm" fw={600} lineClamp={2} style={{ overflowWrap: "anywhere" }}>
        {args.subject || "(no subject)"}
      </Text>
      <Text size="sm" lineClamp={3} style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
        {args.body || "(empty body)"}
      </Text>
      {args.thread_id && (
        <Text size="xs" c="dimmed" lineClamp={1} style={{ overflowWrap: "anywhere" }}>
          Replying to thread {args.thread_id}
        </Text>
      )}
    </Stack>
  );
}

function DraftOpened({ args }: { args: DraftArguments }): JSX.Element {
  return (
    <Stack gap="xs">
      <Text size="sm" style={{ overflowWrap: "anywhere" }}>
        To {recipients(args)}
      </Text>
      {args.cc && args.cc.length > 0 && (
        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
          Cc {args.cc.join(", ")}
        </Text>
      )}
      {args.bcc && args.bcc.length > 0 && (
        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
          Bcc {args.bcc.join(", ")}
        </Text>
      )}
      {args.thread_id && (
        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
          Replying to thread {args.thread_id}
        </Text>
      )}
      <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
        {args.subject || "(no subject)"}
      </Text>
      <TextBlock text={args.body} heading="Message body" format="prose" clamp={false} />
    </Stack>
  );
}

function DraftDetails({ args }: { args: DraftArguments }): JSX.Element {
  return <DraftOpened args={args} />;
}

export const gmailDraftLabel: ArgumentsPreview = definePreview(draftArguments, DraftLabel);
export const gmailDraftCollapsed: ArgumentsPreview = definePreview(draftArguments, DraftCollapsed);
export const gmailDraftOpened: ArgumentsPreview = definePreview(draftArguments, DraftOpened);
export const gmailDraftDetails: ArgumentsPreview = definePreview(draftArguments, DraftDetails);

const threadSearchArguments = z.strictObject({
  q: z.string().nullable().optional(),
  maxResults: z.int().positive().nullable().optional(),
  pageToken: z.string().nullable().optional(),
  includeSpamTrash: z.boolean().nullable().optional(),
});

type ThreadSearchArguments = z.infer<typeof threadSearchArguments>;

/** The compact card shows only the query, so a page token or spam/trash search needs the full card. */
export function offersCardDecisionButtonsThreadSearch(args: unknown): boolean {
  const parsed = threadSearchArguments.safeParse(args);
  return parsed.success && !parsed.data.pageToken && !parsed.data.includeSpamTrash;
}

function ThreadSearchLabel({ args }: { args: ThreadSearchArguments }): JSX.Element {
  void args;
  return <Text fw={600}>Gmail: Search threads</Text>;
}

function ThreadSearchCollapsed({ args }: { args: ThreadSearchArguments }): JSX.Element {
  return (
    <Stack gap="xs">
      <Text size="sm" style={{ overflowWrap: "anywhere" }}>
        {args.q ? (
          <>
            Search for <Code>{args.q}</Code>
          </>
        ) : (
          "Search all threads"
        )}
        {args.pageToken ? " · continuation page" : ""}
      </Text>
      {args.includeSpamTrash === true && (
        <Text size="sm" c="dimmed">
          Including spam and trash
        </Text>
      )}
    </Stack>
  );
}

function ThreadSearchOpened({ args }: { args: ThreadSearchArguments }): JSX.Element {
  return <JsonBlock value={args} clamp={false} />;
}

export const gmailThreadSearchLabel: ArgumentsPreview = definePreview(threadSearchArguments, ThreadSearchLabel);
export const gmailThreadSearchCollapsed: ArgumentsPreview = definePreview(threadSearchArguments, ThreadSearchCollapsed);
export const gmailThreadSearchOpened: ArgumentsPreview = definePreview(threadSearchArguments, ThreadSearchOpened);

const draftResult = z
  .object({
    id: z.string().nullable().optional(),
    message: z
      .object({
        payload: z
          .object({
            headers: z
              .array(
                z
                  .object({
                    name: z.string().nullable().optional(),
                    value: z.string().nullable().optional(),
                  })
                  .passthrough()
              )
              .nullable()
              .optional(),
          })
          .passthrough()
          .nullable()
          .optional(),
      })
      .passthrough()
      .nullable()
      .optional(),
  })
  .passthrough();

type DraftCallResult = z.infer<typeof draftResult>;

function draftSubject(result: DraftCallResult): string | null {
  return result.message?.payload?.headers?.find((header) => header.name?.toLowerCase() === "subject")?.value ?? null;
}

function DraftCall({ args, result }: CallPreviewProps<DraftArguments, DraftCallResult>): JSX.Element {
  const subject = result ? (draftSubject(result) ?? args.subject) : args.subject;
  const draftHref = result?.id
    ? `https://mail.google.com/mail/u/0/#drafts?compose=${encodeURIComponent(result.id)}`
    : null;
  const threadHref = args.thread_id
    ? `https://mail.google.com/mail/u/0/#all/${encodeURIComponent(args.thread_id)}`
    : null;

  return (
    <Stack gap="xs">
      {draftHref ? (
        <Anchor href={draftHref} target="_blank" rel="noreferrer" fw={600}>
          {subject}
        </Anchor>
      ) : (
        <Text fw={600}>{subject}</Text>
      )}
      <Text size="sm" style={{ overflowWrap: "anywhere" }}>
        To {recipients(args)}
      </Text>
      {args.cc && args.cc.length > 0 && (
        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
          Cc {args.cc.join(", ")}
        </Text>
      )}
      {args.bcc && args.bcc.length > 0 && (
        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
          Bcc {args.bcc.join(", ")}
        </Text>
      )}
      <TextBlock text={args.body} heading="Message body" format="prose" />
      {threadHref && (
        <Anchor href={threadHref} target="_blank" rel="noreferrer">
          Reply in Gmail thread
        </Anchor>
      )}
      {result?.id && <Code>draft {result.id}</Code>}
    </Stack>
  );
}

export const gmailDraftCall: CallPreview = defineCallPreview(draftArguments, draftResult, DraftCall);

function DraftResult({ result }: { result: z.infer<typeof draftResult> }): JSX.Element {
  const href = result.id
    ? `https://mail.google.com/mail/u/0/#drafts?compose=${encodeURIComponent(result.id)}`
    : undefined;
  return (
    <Group gap="xs">
      <Text size="sm">Draft created.</Text>
      {href && (
        <Anchor href={href} target="_blank" rel="noreferrer">
          Open in Gmail
        </Anchor>
      )}
      {result.id && <Code>draft {result.id}</Code>}
    </Group>
  );
}

export const gmailDraftResult: ResultPreview = defineResultPreview(draftResult, DraftResult);

const gmailThreadsResultSchema = z
  .object({
    threads: z
      .array(
        z.object({ id: z.string().nullable().optional(), snippet: z.string().nullable().optional() }).passthrough()
      )
      .nullable()
      .optional(),
    nextPageToken: z.string().nullable().optional(),
  })
  .passthrough();

function GmailThreadsResult({ result }: { result: z.infer<typeof gmailThreadsResultSchema> }): JSX.Element {
  const threads = result.threads ?? [];
  return (
    <Stack gap="xs">
      {threads.length === 0 ? (
        <Text size="sm" c="dimmed">
          No threads found.
        </Text>
      ) : (
        threads.map((thread, index) => {
          const text = thread.snippet || thread.id || `Thread ${index + 1}`;
          const href = thread.id ? `https://mail.google.com/mail/u/0/#all/${encodeURIComponent(thread.id)}` : undefined;
          return href ? (
            <Anchor key={thread.id} href={href} target="_blank" rel="noreferrer">
              {text}
            </Anchor>
          ) : (
            <Text key={index} size="sm">
              {text}
            </Text>
          );
        })
      )}
      {result.nextPageToken && (
        <Text size="xs" c="dimmed">
          More threads available.
        </Text>
      )}
    </Stack>
  );
}

export const gmailThreadsResult: ResultPreview = defineResultPreview(gmailThreadsResultSchema, GmailThreadsResult);

// These static views need no Gmail read lookup. Subject/label resolution for thread and message
// actions stays on the submitted-data fallback until Agentplane has an explicit lookup surface.
export const gmailDraftPresentation: ActionPresentation = {
  label: gmailDraftLabel,
  call: gmailDraftCall,
  pane: { collapsed: gmailDraftCollapsed, opened: gmailDraftOpened },
  details: { arguments: gmailDraftDetails, result: gmailDraftResult },
  offersCardDecisionButtons: offersCardDecisionButtonsDraftsCreate,
};

export const gmailThreadSearchPresentation: ActionPresentation = {
  label: gmailThreadSearchLabel,
  pane: { collapsed: gmailThreadSearchCollapsed, opened: gmailThreadSearchOpened },
  details: { arguments: gmailThreadSearchOpened, result: gmailThreadsResult },
  offersCardDecisionButtons: offersCardDecisionButtonsThreadSearch,
};

export const gmailRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  ["drafts_create", gmailDraftPresentation],
  ["threads_list", gmailThreadSearchPresentation],
]);
