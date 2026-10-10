import { Anchor, Code, Group, Stack, Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import { CodeBlock } from "../../code_block";
import { Chip } from "./chips";
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

function recipients(args: DraftArguments): string {
  return args.to.join(", ");
}

function DraftLabel(): JSX.Element {
  return <Text fw={600}>Gmail: Draft email</Text>;
}

function DraftCollapsed({ args }: { args: DraftArguments }): JSX.Element {
  return (
    <Text size="sm" style={{ overflowWrap: "anywhere" }}>
      To {recipients(args)}
    </Text>
  );
}

function DraftOpened({ args }: { args: DraftArguments }): JSX.Element {
  return (
    <Stack gap="xs">
      <Chip label="To" value={recipients(args)} />
      {args.cc && args.cc.length > 0 && <Chip label="Cc" value={args.cc.join(", ")} />}
      {args.bcc && args.bcc.length > 0 && <Chip label="Bcc" value={args.bcc.join(", ")} />}
      {args.thread_id && <Chip label="Reply thread" value={args.thread_id} />}
      <Text size="sm" lineClamp={3} style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
        {args.body}
      </Text>
    </Stack>
  );
}

function DraftDetails({ args }: { args: DraftArguments }): JSX.Element {
  return (
    <Stack gap="xs">
      <Chip label="To" value={recipients(args)} />
      {args.cc && args.cc.length > 0 && <Chip label="Cc" value={args.cc.join(", ")} />}
      {args.bcc && args.bcc.length > 0 && <Chip label="Bcc" value={args.bcc.join(", ")} />}
      {args.thread_id && <Chip label="Reply thread" value={args.thread_id} />}
      <CodeBlock text={args.body} />
    </Stack>
  );
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

function ThreadSearchLabel({ args }: { args: ThreadSearchArguments }): JSX.Element {
  void args;
  return <Text fw={600}>Gmail: Search threads</Text>;
}

function ThreadSearchCollapsed({ args }: { args: ThreadSearchArguments }): JSX.Element {
  return args.q ? (
    <Text size="sm" lineClamp={1}>
      Search: <Code>{args.q}</Code>
    </Text>
  ) : (
    <Text size="sm" c="dimmed">
      Search all threads.
    </Text>
  );
}

function ThreadSearchOpened({ args }: { args: ThreadSearchArguments }): JSX.Element {
  return (
    <Stack gap="xs">
      {args.q && <Chip label="Search" value={args.q} />}
      {args.maxResults != null && <Chip label="Maximum results" value={args.maxResults} />}
      {args.pageToken && <Chip label="Page token" value={args.pageToken} />}
      {args.includeSpamTrash != null && <Chip label="Include spam and trash" value={String(args.includeSpamTrash)} />}
      {!args.q && args.maxResults == null && !args.pageToken && args.includeSpamTrash == null && (
        <Text size="sm" c="dimmed">
          Search all threads.
        </Text>
      )}
    </Stack>
  );
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
      <Chip label="To" value={recipients(args)} />
      {args.cc && args.cc.length > 0 && <Chip label="Cc" value={args.cc.join(", ")} />}
      {args.bcc && args.bcc.length > 0 && <Chip label="Bcc" value={args.bcc.join(", ")} />}
      <CodeBlock text={args.body} />
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
