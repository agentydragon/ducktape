import { Code, Stack, Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import { defineCallPreview, definePreview, type CallPreviewProps, type PreviewProps } from "../entry";
import { expandsForRejectedArguments, type ActionPresentation } from "../presentation";
import { defineResultPreview, type ResultPreviewProps } from "../result_entry";
import { EmailField, emptyArguments, gmail, GmailLink, idArguments, systemLabels, useGmailSummaries } from "./common";

const labelVisibility = z.enum(["labelShow", "labelShowIfUnread", "labelHide"]);
const messageVisibility = z.enum(["show", "hide"]);
const sidebarStates: Record<z.infer<typeof labelVisibility>, string> = {
  labelShow: "Always show",
  labelShowIfUnread: "Show when unread",
  labelHide: "Hide",
};
const messageStates: Record<z.infer<typeof messageVisibility>, string> = { show: "Show", hide: "Hide" };

const labelResult = z.object({
  id: z.string().min(1),
  name: z.string().min(1),
  type: z.enum(["system", "user"]).nullish(),
  labelListVisibility: labelVisibility.nullish(),
  messageListVisibility: messageVisibility.nullish(),
  messagesTotal: z.int().nonnegative().nullish(),
  messagesUnread: z.int().nonnegative().nullish(),
  threadsTotal: z.int().nonnegative().nullish(),
  threadsUnread: z.int().nonnegative().nullish(),
});

type Label = z.infer<typeof labelResult>;
const systemQueries = new Map([
  ["INBOX", "in:inbox"],
  ["SENT", "in:sent"],
  ["DRAFT", "in:drafts"],
  ["TRASH", "in:trash"],
  ["SPAM", "in:spam"],
  ["STARRED", "is:starred"],
  ["UNREAD", "is:unread"],
  ["IMPORTANT", "is:important"],
  ["CATEGORY_PERSONAL", "category:primary"],
  ["CATEGORY_SOCIAL", "category:social"],
  ["CATEGORY_PROMOTIONS", "category:promotions"],
  ["CATEGORY_UPDATES", "category:updates"],
  ["CATEGORY_FORUMS", "category:forums"],
]);

function labelHref(id: string, name: string): string {
  const query = systemQueries.get(id) ?? `label:"${name.replaceAll('"', '\"')}"`;
  return `${gmail}#search/${encodeURIComponent(query)}`;
}

function LabelTarget({ id, requestId }: { id: string; requestId?: string }): JSX.Element {
  const system = systemLabels.get(id);
  const summaries = useGmailSummaries(system ? undefined : requestId);
  const name = system ?? summaries?.labels.find((label) => label.id === id)?.title;
  return name ? <GmailLink href={labelHref(id, name)}>{name}</GmailLink> : <Code>{id}</Code>;
}

function Visibility({
  sidebar,
  messages,
}: {
  sidebar?: Label["labelListVisibility"];
  messages?: Label["messageListVisibility"];
}): JSX.Element {
  return (
    <>
      <EmailField name="Gmail sidebar" value={sidebar ? sidebarStates[sidebar] : undefined} />
      <EmailField name="In message lists" value={messages ? messageStates[messages] : undefined} />
    </>
  );
}

function Counts({
  messages,
  unread,
  label,
}: {
  messages?: number | null;
  unread?: number | null;
  label: string;
}): JSX.Element | null {
  if (messages == null && unread == null) return null;
  const parts = [messages == null ? undefined : `${messages} total`, unread == null ? undefined : `${unread} unread`];
  return <EmailField name={label} value={parts.filter(Boolean).join(", ")} />;
}

function LabelCard({ result }: ResultPreviewProps<Label>): JSX.Element {
  return (
    <Stack gap={4}>
      <Text size="sm" fw={600}>
        <GmailLink href={labelHref(result.id, result.name)}>{systemLabels.get(result.id) ?? result.name}</GmailLink>
      </Text>
      <Visibility sidebar={result.labelListVisibility} messages={result.messageListVisibility} />
      <Counts label="Threads" messages={result.threadsTotal} unread={result.threadsUnread} />
      <Counts label="Messages" messages={result.messagesTotal} unread={result.messagesUnread} />
    </Stack>
  );
}

const createArguments = z.strictObject({
  name: z.string().min(1),
  label_list_visibility: labelVisibility.default("labelShow"),
  message_list_visibility: messageVisibility.default("show"),
});
const labelMutationArguments = z.strictObject({ label_id: z.string().min(1) });
const patchArguments = labelMutationArguments.extend({
  name: z.string().min(1).nullish(),
  label_list_visibility: labelVisibility.nullish(),
  message_list_visibility: messageVisibility.nullish(),
});
type LabelPatch = z.infer<typeof patchArguments>;

function PatchFields({ args }: { args: LabelPatch }): JSX.Element {
  return (
    <Stack gap={4}>
      <EmailField name="Rename to" value={args.name} />
      <Visibility sidebar={args.label_list_visibility} messages={args.message_list_visibility} />
      {args.name == null && args.label_list_visibility == null && args.message_list_visibility == null && (
        <Text size="sm">No name or visibility change requested.</Text>
      )}
    </Stack>
  );
}

const createLabelPreview = definePreview(createArguments, ({ args }) => (
  <Stack gap={4}>
    <EmailField name="Name" value={args.name} />
    <Visibility sidebar={args.label_list_visibility} messages={args.message_list_visibility} />
  </Stack>
));

function PatchLabel({ args, requestId }: PreviewProps<LabelPatch>): JSX.Element {
  return (
    <Stack gap={4}>
      <Text size="sm">
        <LabelTarget id={args.label_id} requestId={requestId} />
      </Text>
      <PatchFields args={args} />
    </Stack>
  );
}

function PatchLabelCall({ args, result, requestId }: CallPreviewProps<LabelPatch, Label>): JSX.Element {
  return result === undefined ? (
    <PatchLabel args={args} requestId={requestId} />
  ) : (
    <Stack gap="xs">
      <Text size="sm">Gmail saved the label changes.</Text>
      <LabelCard result={result} />
    </Stack>
  );
}

const patchLabelPreview = definePreview(patchArguments, PatchLabel);
const deleteLabelPreview = definePreview(labelMutationArguments, ({ args, requestId }) => (
  <Stack gap={4}>
    <Text size="sm">
      <LabelTarget id={args.label_id} requestId={requestId} />
    </Text>
    <Text size="sm">Removes the label from every thread that has it.</Text>
  </Stack>
));
const getLabelPreview = definePreview(idArguments, ({ args, requestId }) => (
  <Text size="sm">
    <LabelTarget id={args.id} requestId={requestId} />
  </Text>
));
const getLabelCall = defineCallPreview(idArguments, labelResult, ({ args, result, requestId }) =>
  result === undefined ? (
    <Text size="sm">
      <LabelTarget id={args.id} requestId={requestId} />
    </Text>
  ) : (
    <LabelCard result={result} />
  )
);
const labelsResult = z.strictObject({ labels: z.array(labelResult).nullish() });
const labelsView = defineResultPreview(labelsResult, ({ result }) => (
  <Stack gap="xs">
    <Text size="sm" c="dimmed">
      {result.labels?.length ? `${result.labels.length} labels` : "No labels returned."}
    </Text>
    {result.labels?.map((label) => (
      <LabelCard key={label.id} result={label} />
    ))}
  </Stack>
));

export const labelRenderers: ReadonlyMap<string, ActionPresentation> = new Map<string, ActionPresentation>([
  [
    "labels_list",
    {
      fallbackLabel: "List Gmail labels",
      pane: { collapsed: null },
      details: { arguments: { schema: emptyArguments, render: () => null }, result: labelsView },
      compactCardExpands: expandsForRejectedArguments(emptyArguments),
      offersCardDecisionButtons: (args) => emptyArguments.safeParse(args).success,
    },
  ],
  [
    "labels_get",
    {
      fallbackLabel: "Read Gmail label",
      pane: { collapsed: null, opened: getLabelPreview },
      call: getLabelCall,
      details: { arguments: getLabelPreview, result: defineResultPreview(labelResult, LabelCard) },
    },
  ],
  [
    "labels_create",
    {
      fallbackLabel: "Create Gmail label",
      pane: { collapsed: createLabelPreview },
      details: {
        arguments: createLabelPreview,
        result: defineResultPreview(labelResult, ({ result }) => (
          <Stack gap="xs">
            <Text size="sm">Gmail created the label.</Text>
            <LabelCard result={result} />
          </Stack>
        )),
      },
      compactCardExpands: expandsForRejectedArguments(createArguments),
      offersCardDecisionButtons: (args) => createArguments.safeParse(args).success,
    },
  ],
  [
    "labels_patch",
    {
      fallbackLabel: "Update Gmail label",
      call: defineCallPreview(patchArguments, labelResult, PatchLabelCall),
      pane: { collapsed: definePreview(patchArguments, PatchFields), opened: patchLabelPreview },
      details: { arguments: patchLabelPreview, result: defineResultPreview(labelResult, LabelCard) },
    },
  ],
  [
    "labels_delete",
    {
      fallbackLabel: "Delete Gmail label",
      pane: {
        collapsed: definePreview(labelMutationArguments, () => (
          <Text size="sm">Affects every thread with the label.</Text>
        )),
        opened: deleteLabelPreview,
      },
      details: { arguments: deleteLabelPreview, emptyResult: <Text size="sm">Label deleted.</Text> },
    },
  ],
]);
