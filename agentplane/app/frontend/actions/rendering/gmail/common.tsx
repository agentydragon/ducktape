import { Anchor, Code, Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import type { JSX, ReactNode } from "react";
import { z } from "zod";

import type { GetGmailResourceSummariesResponse } from "../../../../../action_service/api/contracts/operator_action_service_pb";
import { getGmailResourceSummaries } from "../../client";

export const gmail = "https://mail.google.com/mail/u/0/";
export const threadHref = (id: string) => `${gmail}#all/${encodeURIComponent(id)}`;
export const draftHref = (id: string) => `${gmail}#drafts?compose=${encodeURIComponent(id)}`;
export const wrap = { overflowWrap: "anywhere" } as const;

export function GmailLink({ href, children }: { href: string; children: ReactNode }): JSX.Element {
  return (
    <Anchor href={href} target="_blank" rel="noreferrer" inherit style={wrap}>
      {children}
    </Anchor>
  );
}

export function ThreadTarget({ id, subject }: { id: string; subject?: string | null }): JSX.Element {
  return <GmailLink href={threadHref(id)}>{subject || <Code>{id}</Code>}</GmailLink>;
}

export const systemLabels: ReadonlyMap<string, string> = new Map([
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

export function EmailField({ name, value }: { name: string; value?: string | null }): JSX.Element | null {
  return value ? (
    <Text size="sm" style={wrap}>
      <Text component="span" fw={600} inherit>
        {name}
      </Text>{" "}
      {value}
    </Text>
  ) : null;
}

export const emptyArguments: z.ZodType<Record<string, never>> = z.strictObject({});
export const idArguments: z.ZodObject<{ id: z.ZodString }> = z.strictObject({ id: z.string().min(1) });
export const listArguments: z.ZodObject<{
  q: z.ZodOptional<z.ZodNullable<z.ZodString>>;
  maxResults: z.ZodOptional<z.ZodNullable<z.ZodNumber>>;
  pageToken: z.ZodOptional<z.ZodNullable<z.ZodString>>;
  includeSpamTrash: z.ZodOptional<z.ZodNullable<z.ZodBoolean>>;
}> = z.strictObject({
  q: z.string().nullish(),
  maxResults: z.int().nonnegative().nullish(),
  pageToken: z.string().nullish(),
  includeSpamTrash: z.boolean().nullish(),
});

export function useGmailSummaries(requestId?: string): GetGmailResourceSummariesResponse | undefined {
  return useQuery({
    queryKey: ["action-gmail-resource-summaries", requestId],
    queryFn: ({ signal }) => getGmailResourceSummaries(requestId ?? "", signal),
    enabled: Boolean(requestId),
    retry: false,
    staleTime: 30_000,
  }).data;
}

export function labelName(id: string, labels?: readonly { id: string; title: string }[]): string {
  return systemLabels.get(id) ?? labels?.find((label) => label.id === id)?.title ?? id;
}
