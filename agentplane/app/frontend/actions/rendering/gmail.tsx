import { Anchor, Code, Stack, Text } from "@mantine/core";
import type { JSX, ReactNode } from "react";
import { z } from "zod";

import { TextBlock } from "../../ui/text_block";
import { modifyThreadLabelsArguments } from "../schemas/integration_previews";
import { defineCallPreview, definePreview, type CallPreviewProps, type PreviewProps } from "./entry";
import { useIntegrationPreviewNames } from "./integration_previews";
import { expandsForRejectedArguments, type ActionPresentation } from "./presentation";
import { defineResultPreview, type ResultPreviewProps } from "./result_entry";

const gmail = "https://mail.google.com/mail/u/0/";
const threadHref = (id: string) => `${gmail}#all/${encodeURIComponent(id)}`;
const draftHref = (id: string) => `${gmail}#drafts?compose=${encodeURIComponent(id)}`;
const wrap = { overflowWrap: "anywhere" } as const;

function GmailLink({ href, children }: { href: string; children: ReactNode }): JSX.Element {
  return (
    <Anchor href={href} target="_blank" rel="noreferrer" inherit style={wrap}>
      {children}
    </Anchor>
  );
}

function ThreadTarget({ id, subject }: { id: string; subject?: string | null }): JSX.Element {
  return <GmailLink href={threadHref(id)}>{subject || <Code>{id}</Code>}</GmailLink>;
}

const systemLabels: ReadonlyMap<string, string> = new Map([
  ["INBOX", "Inbox"],
  ["SPAM", "Spam"],
  ["TRASH", "Trash"],
  ["UNREAD", "Unread"],
  ["STARRED", "Starred"],
  ["IMPORTANT", "Important"],
  ["SENT", "Sent"],
  ["DRAFT", "Drafts"],
  ["CATEGORY_PERSONAL", "Primary"],
  ["CATEGORY_SOCIAL", "Social"],
  ["CATEGORY_PROMOTIONS", "Promotions"],
  ["CATEGORY_UPDATES", "Updates"],
  ["CATEGORY_FORUMS", "Forums"],
]);

function EmailField({ name, value }: { name: string; value?: string | null }): JSX.Element | null {
  return value ? (
    <Text size="sm" style={wrap}>
      <Text component="span" fw={600} inherit>
        {name}
      </Text>{" "}
      {value}
    </Text>
  ) : null;
}

const draftArguments = z.strictObject({
  to: z.array(z.string()).min(1),
  subject: z.string(),
  body: z.string(),
  cc: z.array(z.string()).nullish(),
  bcc: z.array(z.string()).nullish(),
  thread_id: z.string().nullish(),
});

type DraftArguments = z.infer<typeof draftArguments>;

const headers = z.array(z.object({ name: z.string(), value: z.string() }));
const messageSummary = z.object({
  snippet: z.string().nullish(),
  payload: z.object({ headers: headers.nullish() }).nullish(),
});
const draftResult = z.object({ id: z.string().min(1), message: messageSummary.nullish() });

function header(message: z.infer<typeof messageSummary> | null | undefined, name: string): string | undefined {
  return message?.payload?.headers?.find((item) => item.name.toLowerCase() === name)?.value;
}

function DraftFields({
  args,
  short = false,
  clamp = false,
}: {
  args: DraftArguments;
  short?: boolean;
  clamp?: boolean;
}): JSX.Element {
  return (
    <>
      <EmailField name="To" value={args.to.join(", ")} />
      <EmailField name="Cc" value={args.cc?.join(", ")} />
      <EmailField name="Bcc" value={args.bcc?.join(", ")} />
      {short ? (
        <Text size="sm" lineClamp={3} style={{ ...wrap, whiteSpace: "pre-wrap" }}>
          {args.body || "(empty body)"}
        </Text>
      ) : (
        <TextBlock text={args.body || "(empty body)"} heading="Message body" format="prose" clamp={clamp} />
      )}
    </>
  );
}

function DraftCollapsed({ args }: PreviewProps<DraftArguments>): JSX.Element {
  return (
    <Stack gap={4}>
      <Text size="sm" fw={600} style={wrap}>
        {args.subject || "(no subject)"}
      </Text>
      <DraftFields args={args} short />
    </Stack>
  );
}

function DraftReply({ id, requestId }: { id: string; requestId?: string }): JSX.Element {
  const names = useIntegrationPreviewNames("gmail", requestId);
  return (
    <Text size="sm">
      Reply in thread <ThreadTarget id={id} subject={names?.get(id)} />
    </Text>
  );
}

function DraftCall({
  args,
  result,
  requestId,
}: CallPreviewProps<DraftArguments, z.infer<typeof draftResult>>): JSX.Element {
  const subject = header(result?.message, "subject") || args.subject || "(no subject)";
  return (
    <Stack gap="xs">
      <Text size="sm" fw={600} style={wrap}>
        {result ? <GmailLink href={draftHref(result.id)}>{subject}</GmailLink> : subject}
      </Text>
      <Text size="sm" c="dimmed">
        {result ? "Saved as an unsent draft." : "Creates an unsent draft."}
      </Text>
      {args.thread_id && <DraftReply id={args.thread_id} requestId={requestId} />}
      <DraftFields args={args} clamp={result !== undefined} />
    </Stack>
  );
}

const draftPreview = definePreview(draftArguments, DraftCallArguments);
function DraftCallArguments({ args, requestId }: PreviewProps<DraftArguments>): JSX.Element {
  return <DraftCall args={args} requestId={requestId} result={undefined} />;
}

const draftPresentation: ActionPresentation = {
  fallbackLabel: "Gmail: Draft email",
  call: defineCallPreview(draftArguments, draftResult, DraftCall),
  pane: { collapsed: definePreview(draftArguments, DraftCollapsed), opened: draftPreview },
  details: {
    arguments: draftPreview,
    result: defineResultPreview(draftResult, ({ result }) => (
      <Text size="sm">
        Saved as an unsent draft.{" "}
        <GmailLink href={draftHref(result.id)}>{header(result.message, "subject") || "Open draft in Gmail"}</GmailLink>
      </Text>
    )),
  },
  offersCardDecisionButtons: (args) => {
    const parsed = draftArguments.safeParse(args);
    return parsed.success && !parsed.data.thread_id;
  },
};

const searchArguments = z.strictObject({
  q: z.string().nullish(),
  labelIds: z.array(z.string()).nullish(),
  maxResults: z.int().nonnegative().nullish(),
  pageToken: z.string().nullish(),
  includeSpamTrash: z.boolean().nullish(),
});

function SearchScope({ args }: PreviewProps<z.infer<typeof searchArguments>>): JSX.Element {
  return (
    <Stack gap={4}>
      {args.q ? <EmailField name="Query" value={args.q} /> : <Text size="sm">All threads</Text>}
      <EmailField name="All labels" value={args.labelIds?.map((id) => systemLabels.get(id) ?? id).join(", ")} />
      {args.maxResults != null && <Text size="sm">Up to {args.maxResults} threads</Text>}
      {args.includeSpamTrash && <Text size="sm">Includes spam and trash</Text>}
      {args.pageToken && <Text size="sm">Continues a previous search</Text>}
    </Stack>
  );
}

const searchPreview = definePreview(searchArguments, SearchScope);
const searchResult = z
  .object({
    threads: z
      .array(
        z.object({
          id: z.string().min(1),
          snippet: z.string().nullish(),
          messages: z.array(messageSummary).nullish(),
        })
      )
      .nullish(),
    nextPageToken: z.string().nullish(),
    resultSizeEstimate: z.int().nonnegative().nullish(),
  })
  .refine((result) => result.threads != null || result.resultSizeEstimate != null);

function ThreadResults({ result, context }: ResultPreviewProps<z.infer<typeof searchResult>>): JSX.Element {
  const threads = result.threads ?? [];
  const names = useIntegrationPreviewNames(
    "gmail",
    threads.some((thread) => !header(thread.messages?.[0], "subject")) ? context?.requestId : undefined
  );
  return (
    <Stack gap="xs">
      <Text size="sm" c="dimmed">
        {threads.length === 0
          ? "No threads returned."
          : `${threads.length} ${threads.length === 1 ? "thread" : "threads"} returned`}
      </Text>
      {threads.map((thread) => {
        const message = thread.messages?.[0];
        const snippet = thread.snippet || message?.snippet;
        const title = header(message, "subject") || names?.get(thread.id) || snippet;
        return (
          <Stack key={thread.id} gap={2}>
            <Text size="sm" fw={600}>
              <ThreadTarget id={thread.id} subject={title} />
            </Text>
            <EmailField name="From" value={header(message, "from")} />
            <EmailField name="Date" value={header(message, "date")} />
            {snippet && title !== snippet && (
              <Text size="sm" c="dimmed" style={wrap}>
                {snippet}
              </Text>
            )}
          </Stack>
        );
      })}
      {result.nextPageToken && <Text size="sm">More threads available.</Text>}
    </Stack>
  );
}

const searchPresentation: ActionPresentation = {
  fallbackLabel: "Gmail: Search threads",
  pane: { collapsed: searchPreview },
  details: { arguments: searchPreview, result: defineResultPreview(searchResult, ThreadResults) },
  compactCardExpands: expandsForRejectedArguments(searchArguments),
  offersCardDecisionButtons: (args) => searchArguments.safeParse(args).success,
};

function LabelChanges({ add, remove }: { add?: string[] | null; remove?: string[] | null }): JSX.Element {
  return (
    <>
      <EmailField name="Add" value={add?.join(", ")} />
      <EmailField name="Remove" value={remove?.join(", ")} />
    </>
  );
}

function ThreadsRelabel({ args, requestId }: PreviewProps<z.infer<typeof modifyThreadLabelsArguments>>): JSX.Element {
  const names = useIntegrationPreviewNames("gmail", requestId);
  return (
    <Stack gap={4}>
      <LabelChanges add={args.add} remove={args.remove} />
      {args.add?.length ? (
        <Text size="sm" c="dimmed">
          Creates any missing labels in Add.
        </Text>
      ) : null}
      {!args.add?.length && !args.remove?.length && <Text size="sm">No label changes specified.</Text>}
      {args.thread_ids.map((id, index) => (
        <Text size="sm" key={index}>
          <ThreadTarget id={id} subject={names?.get(id)} />
        </Text>
      ))}
    </Stack>
  );
}

const labelRef = z.object({ id: z.string(), name: z.string() });
const relabelResult = z.object({
  thread_count: z.int().nonnegative(),
  added: z.array(labelRef),
  removed: z.array(labelRef),
});
const relabelPreview = definePreview(modifyThreadLabelsArguments, ThreadsRelabel);
const relabelPresentation: ActionPresentation = {
  fallbackLabel: "Change Gmail thread labels",
  pane: {
    collapsed: definePreview(modifyThreadLabelsArguments, ({ args }) => (
      <Text size="sm">
        {args.thread_ids.length} {args.thread_ids.length === 1 ? "thread" : "threads"}
      </Text>
    )),
    opened: relabelPreview,
  },
  details: {
    arguments: relabelPreview,
    result: defineResultPreview(relabelResult, ({ result }) => (
      <Stack gap={4}>
        <Text size="sm">
          Updated labels on {result.thread_count} {result.thread_count === 1 ? "thread" : "threads"}.
        </Text>
        <EmailField name="Added" value={result.added.map(({ name }) => name).join(", ")} />
        <EmailField name="Removed" value={result.removed.map(({ name }) => name).join(", ")} />
      </Stack>
    )),
  },
};

export const gmailRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  ["drafts_create", draftPresentation],
  ["threads_list", searchPresentation],
  ["threads_modify_labels", relabelPresentation],
]);
