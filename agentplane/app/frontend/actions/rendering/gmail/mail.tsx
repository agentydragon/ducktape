import { Code, Stack, Text } from "@mantine/core";
import DOMPurify from "dompurify";
import type { JSX } from "react";
import { z } from "zod";

import { TextBlock } from "../../../ui/text_block";
import { defineCallPreview, definePreview, type CallPreviewProps, type PreviewProps } from "../entry";
import { useIntegrationPreviewNames } from "../integration_previews";
import { expandsForRejectedArguments, type ActionPresentation } from "../presentation";
import { defineResultPreview, type ResultPreviewProps } from "../result_entry";
import {
  draftHref,
  EmailField,
  GmailLink,
  idArguments,
  labelName,
  listArguments,
  ThreadTarget,
  threadHref,
  useGmailSummaries,
  wrap,
} from "./common";

type MessageHeader = { name: string; value: string };
const messageHeaders: z.ZodType<MessageHeader[]> = z.array(z.object({ name: z.string(), value: z.string() }));

interface MessagePart {
  mimeType?: string | null;
  filename?: string | null;
  headers?: MessageHeader[] | null;
  body?: { data?: string | null; attachmentId?: string | null } | null;
  parts?: MessagePart[] | null;
}

const part: z.ZodType<MessagePart> = z.object({
  mimeType: z.string().nullish(),
  filename: z.string().nullish(),
  headers: messageHeaders.nullish(),
  body: z.object({ data: z.string().nullish(), attachmentId: z.string().nullish() }).nullish(),
  get parts() {
    return z.array(part).nullish();
  },
});

interface Message {
  id: string;
  threadId?: string | null;
  snippet?: string | null;
  internalDate?: string | null;
  labelIds?: string[] | null;
  payload?: MessagePart | null;
  raw?: string | null;
}

const messageResult: z.ZodType<Message> = z.object({
  id: z.string().min(1),
  threadId: z.string().nullish(),
  snippet: z.string().nullish(),
  internalDate: z.string().nullish(),
  labelIds: z.array(z.string()).nullish(),
  payload: part.nullish(),
  raw: z.string().nullish(),
});
export const draftResult: z.ZodType<{ id: string; message?: Message | null }> = z.object({
  id: z.string().min(1),
  message: messageResult.nullish(),
});
const threadResult = z.object({
  id: z.string().min(1),
  snippet: z.string().nullish(),
  messages: z.array(messageResult).nullish(),
});

export function messageHeader(
  message: { payload?: { headers?: MessageHeader[] | null } | null } | null | undefined,
  name: string
): string | undefined {
  return findHeader(message?.payload, name);
}

function findHeader(part: { headers?: MessageHeader[] | null } | null | undefined, name: string) {
  return part?.headers?.find((item) => item.name.toLowerCase() === name)?.value;
}

function decodeBase64Url(data: string, charset = "utf-8"): string | undefined {
  try {
    const binary = atob(data.replaceAll("-", "+").replaceAll("_", "/"));
    return new TextDecoder(charset).decode(Uint8Array.from(binary, (char) => char.charCodeAt(0)));
  } catch {
    return undefined;
  }
}

function readableHtml(html: string): string {
  const body = DOMPurify.sanitize(html, {
    ALLOWED_TAGS: ["br", "p", "div", "li", "blockquote", "table", "tr", "td"],
    ALLOWED_ATTR: [],
    RETURN_DOM_FRAGMENT: true,
  });
  for (const br of body.querySelectorAll("br")) br.replaceWith("\n");
  for (const block of body.querySelectorAll("p, div, li, blockquote, tr")) block.append("\n");
  for (const cell of body.querySelectorAll("td")) cell.append(" ");
  return body.textContent?.trim() ?? "";
}

function messageText(payload: MessagePart | null | undefined): string | undefined {
  const plain: string[] = [];
  const html: string[] = [];
  const pending = payload ? [payload] : [];
  while (pending.length) {
    const part = pending.pop();
    if (!part || part.filename) continue;
    if (part.parts) pending.push(...[...part.parts].reverse());
    if (!part.body?.data || (part.mimeType !== "text/plain" && part.mimeType !== "text/html")) continue;
    const charset = /charset\s*=\s*"?([a-z0-9_-]+)/i.exec(findHeader(part, "content-type") ?? "")?.[1];
    const decoded = decodeBase64Url(part.body.data, charset);
    if (decoded) (part.mimeType === "text/html" ? html : plain).push(decoded);
  }
  return plain.length ? plain.join("\n\n") : html.map(readableHtml).join("\n\n") || undefined;
}

function messageDate(message: Message): string | undefined {
  const headerDate = messageHeader(message, "date");
  if (headerDate) return headerDate;
  if (!message.internalDate) return undefined;
  const date = new Date(Number(message.internalDate));
  return Number.isFinite(date.getTime())
    ? date
        .toISOString()
        .replace("T", " ")
        .replace(/\.\d{3}Z$/, " UTC")
    : undefined;
}

function MessageDetails({ message }: { message: Message }): JSX.Element {
  const raw = message.raw ? decodeBase64Url(message.raw) : undefined;
  const body = raw ?? messageText(message.payload) ?? message.snippet;
  return (
    <Stack gap={4}>
      <EmailField name="From" value={messageHeader(message, "from")} />
      <EmailField name="To" value={messageHeader(message, "to")} />
      <EmailField name="Cc" value={messageHeader(message, "cc")} />
      <EmailField name="Bcc" value={messageHeader(message, "bcc")} />
      <EmailField name="Date" value={messageDate(message)} />
      <EmailField name="Labels" value={message.labelIds?.map((id) => labelName(id)).join(", ")} />
      {body && <TextBlock text={body} heading={raw ? "Email source" : "Message"} format="prose" />}
    </Stack>
  );
}

function ThreadView({ result }: ResultPreviewProps<z.infer<typeof threadResult>>): JSX.Element {
  const messages = result.messages ?? [];
  const subject = messageHeader(messages[0], "subject");
  return (
    <Stack gap="xs">
      <Text size="sm" fw={600}>
        <ThreadTarget id={result.id} subject={subject} />
      </Text>
      {messages.length > 0 && (
        <Text size="sm" c="dimmed">
          {messages.length} {messages.length === 1 ? "message" : "messages"}
        </Text>
      )}
      {messages.map((message) => (
        <MessageDetails key={message.id} message={message} />
      ))}
      {messages.length === 0 && result.snippet && (
        <Text size="sm" style={wrap}>
          {result.snippet}
        </Text>
      )}
    </Stack>
  );
}

function MessageView({ result }: ResultPreviewProps<Message>): JSX.Element {
  return (
    <Stack gap="xs">
      <Text size="sm" fw={600}>
        <GmailLink href={threadHref(result.threadId || result.id)}>
          {messageHeader(result, "subject") || "Open message in Gmail"}
        </GmailLink>
      </Text>
      <MessageDetails message={result} />
    </Stack>
  );
}

function DraftView({ result }: ResultPreviewProps<z.infer<typeof draftResult>>): JSX.Element {
  return (
    <Stack gap={4}>
      <Text size="sm" fw={600}>
        <GmailLink href={draftHref(result.id)}>
          {messageHeader(result.message, "subject") || "Open draft in Gmail"}
        </GmailLink>
      </Text>
      <Text size="sm" c="dimmed">
        Unsent draft
      </Text>
      {result.message && <MessageDetails message={result.message} />}
    </Stack>
  );
}

export function DraftTarget({ id, requestId }: { id: string; requestId?: string }): JSX.Element {
  const summaries = useGmailSummaries(requestId);
  const subject = summaries?.drafts.find((draft) => draft.id === id)?.title;
  return subject ? <GmailLink href={draftHref(id)}>{subject}</GmailLink> : <Code>{id}</Code>;
}

const threadArguments = idArguments.extend({
  format: z.enum(["full", "metadata", "minimal"]).optional(),
  metadataHeaders: z.array(z.string()).optional(),
});
const messageArguments = threadArguments.extend({
  format: z.enum(["full", "metadata", "minimal", "raw"]).optional(),
});
const draftGetArguments = idArguments.extend({ format: messageArguments.shape.format });
type MailReadArguments = z.infer<typeof messageArguments>;

function ReadScope({ args }: { args: MailReadArguments }): JSX.Element {
  const labels: Record<NonNullable<MailReadArguments["format"]>, string> = {
    full: "Includes message bodies",
    metadata: "Headers and labels only",
    minimal: "IDs and labels only",
    raw: "Includes full email source",
  };
  return (
    <Stack gap={4}>
      <Text size="sm">{labels[args.format ?? "full"]}</Text>
      {args.format === "metadata" && <EmailField name="Requested headers" value={args.metadataHeaders?.join(", ")} />}
    </Stack>
  );
}

function MailTarget({
  id,
  kind,
  requestId,
}: {
  id: string;
  kind: "thread" | "message" | "draft";
  requestId?: string;
}): JSX.Element {
  const threads = useIntegrationPreviewNames("gmail", kind === "thread" ? requestId : undefined);
  const summaries = useGmailSummaries(kind === "message" ? requestId : undefined);
  if (kind === "draft") return <DraftTarget id={id} requestId={requestId} />;
  const name = kind === "thread" ? threads?.get(id) : summaries?.messages.find((message) => message.id === id)?.title;
  return <ThreadTarget id={id} subject={name} />;
}

function mailRead<Args extends z.ZodType<MailReadArguments>, Result extends z.ZodType>(
  kind: "thread" | "message" | "draft",
  schema: Args,
  resultSchema: Result,
  ResultView: (props: ResultPreviewProps<z.infer<Result>>) => JSX.Element
): ActionPresentation {
  function Review({ args, requestId }: PreviewProps<z.infer<Args>>): JSX.Element {
    return (
      <Stack gap={4}>
        <Text size="sm">
          <MailTarget id={args.id} requestId={requestId} kind={kind} />
        </Text>
        <ReadScope args={args} />
      </Stack>
    );
  }
  function Call({ args, result, requestId }: CallPreviewProps<z.infer<Args>, z.infer<Result>>): JSX.Element {
    return result === undefined ? (
      <Review args={args} requestId={requestId} />
    ) : (
      <Stack gap="xs">
        <ReadScope args={args} />
        <ResultView result={result} />
      </Stack>
    );
  }
  const detail = definePreview(schema, Review);
  return {
    fallbackLabel: `Read Gmail ${kind}`,
    call: defineCallPreview(schema, resultSchema, Call),
    pane: { collapsed: definePreview(schema, ({ args }) => <ReadScope args={args} />), opened: detail },
    details: { arguments: detail, result: defineResultPreview(resultSchema, ResultView) },
  };
}

const draftListPreview = definePreview(listArguments, ({ args }) => (
  <Stack gap={4}>
    {args.q ? <EmailField name="Query" value={args.q} /> : <Text size="sm">All unsent drafts</Text>}
    {args.maxResults != null && <Text size="sm">Up to {args.maxResults} drafts</Text>}
    {args.includeSpamTrash && <Text size="sm">Includes spam and trash</Text>}
    {args.pageToken && <Text size="sm">Continues a previous search</Text>}
  </Stack>
));
const draftListResult = z
  .object({
    drafts: z.array(draftResult).nullish(),
    nextPageToken: z.string().nullish(),
    resultSizeEstimate: z.int().nonnegative().nullish(),
  })
  .refine((result) => result.drafts != null || result.resultSizeEstimate != null);

function DraftList({ result, context }: ResultPreviewProps<z.infer<typeof draftListResult>>): JSX.Element {
  const drafts = result.drafts ?? [];
  const summaries = useGmailSummaries(
    drafts.some((draft) => !messageHeader(draft.message, "subject")) ? context?.requestId : undefined
  );
  return (
    <Stack gap="xs">
      <Text size="sm" c="dimmed">
        {drafts.length === 0
          ? "No drafts returned."
          : `${drafts.length} unsent ${drafts.length === 1 ? "draft" : "drafts"}`}
      </Text>
      {drafts.map((draft) => (
        <Stack key={draft.id} gap={2}>
          <Text size="sm" fw={600}>
            <GmailLink href={draftHref(draft.id)}>
              {messageHeader(draft.message, "subject") ||
                summaries?.drafts.find((item) => item.id === draft.id)?.title || <Code>{draft.id}</Code>}
            </GmailLink>
          </Text>
          <EmailField name="To" value={messageHeader(draft.message, "to")} />
          <EmailField name="Cc" value={messageHeader(draft.message, "cc")} />
          <EmailField name="Bcc" value={messageHeader(draft.message, "bcc")} />
          {draft.message?.snippet && (
            <Text size="sm" c="dimmed" style={wrap}>
              {draft.message.snippet}
            </Text>
          )}
        </Stack>
      ))}
      {result.nextPageToken && <Text size="sm">More drafts available.</Text>}
    </Stack>
  );
}

export const mailReadRenderers: ReadonlyMap<string, ActionPresentation> = new Map<string, ActionPresentation>([
  ["threads_get", mailRead("thread", threadArguments, threadResult, ThreadView)],
  ["messages_get", mailRead("message", messageArguments, messageResult, MessageView)],
  ["drafts_get", mailRead("draft", draftGetArguments, draftResult, DraftView)],
  [
    "drafts_list",
    {
      fallbackLabel: "Search Gmail drafts",
      pane: { collapsed: draftListPreview },
      details: { arguments: draftListPreview, result: defineResultPreview(draftListResult, DraftList) },
      compactCardExpands: expandsForRejectedArguments(listArguments),
      offersCardDecisionButtons: (args) => listArguments.safeParse(args).success,
    },
  ],
]);
