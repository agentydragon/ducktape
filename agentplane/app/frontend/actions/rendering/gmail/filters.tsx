import { Code, Stack, Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import { defineCallPreview, definePreview, type CallPreviewProps, type PreviewProps } from "../entry";
import { expandsForRejectedArguments, type ActionPresentation } from "../presentation";
import { defineResultPreview, type ResultPreviewProps } from "../result_entry";
import {
  EmailField,
  emptyArguments,
  gmail,
  GmailLink,
  idArguments,
  labelName,
  systemLabels,
  useGmailSummaries,
} from "./common";

const criteria = z.strictObject({
  from: z.string().nullish(),
  to: z.string().nullish(),
  subject: z.string().nullish(),
  query: z.string().nullish(),
  negatedQuery: z.string().nullish(),
  hasAttachment: z.boolean().nullish(),
  excludeChats: z.boolean().nullish(),
  size: z.int().nullish(),
  sizeComparison: z.enum(["smaller", "larger"]).nullish(),
});
const effect = z.strictObject({
  addLabelIds: z.array(z.string()).nullish(),
  removeLabelIds: z.array(z.string()).nullish(),
  forward: z.string().nullish(),
});
const filterCreateArguments = z.strictObject({ criteria, action: effect });
const filterResult = z.object({
  id: z.string().min(1),
  criteria: z.object(criteria.shape),
  action: z.object(effect.shape),
});
type Filter = z.infer<typeof filterCreateArguments>;
const filterSettings = `${gmail}#settings/filters`;

function FilterRule({ rule, requestId }: { rule: Filter; requestId?: string }): JSX.Element {
  const { criteria, action } = rule;
  const add = action.addLabelIds ?? [];
  const remove = action.removeLabelIds ?? [];
  const hasCustomLabel = [...add, ...remove].some((id) => !systemLabels.has(id));
  const summaries = useGmailSummaries(hasCustomLabel ? requestId : undefined);
  return (
    <Stack gap={4}>
      <Text size="sm" fw={600}>
        When all conditions match
      </Text>
      <EmailField name="From" value={criteria.from} />
      <EmailField name="To" value={criteria.to} />
      <EmailField name="Subject contains" value={criteria.subject} />
      <EmailField name="Matches query" value={criteria.query} />
      <EmailField name="Excludes query" value={criteria.negatedQuery} />
      {criteria.hasAttachment != null && (
        <Text size="sm">{criteria.hasAttachment ? "Has an attachment" : "Has no attachments"}</Text>
      )}
      {criteria.excludeChats != null && (
        <Text size="sm">{criteria.excludeChats ? "Exclude chats" : "Includes chats"}</Text>
      )}
      {criteria.size != null && (
        <EmailField name="Size" value={`${criteria.sizeComparison ?? ""} ${criteria.size} bytes`.trim()} />
      )}
      {criteria.size == null && criteria.sizeComparison && (
        <EmailField name="Size comparison" value={criteria.sizeComparison} />
      )}
      {!Object.values(criteria).some((value) => value != null) && <Text size="sm">No conditions specified.</Text>}
      <Text size="sm" fw={600}>
        Then
      </Text>
      <EmailField name="Forward to" value={action.forward} />
      <EmailField name="Add labels" value={add.map((id) => labelName(id, summaries?.labels)).join(", ")} />
      <EmailField name="Remove labels" value={remove.map((id) => labelName(id, summaries?.labels)).join(", ")} />
      {!action.forward && add.length === 0 && remove.length === 0 && <Text size="sm">No effects specified.</Text>}
    </Stack>
  );
}

const filterCreatePreview = definePreview(filterCreateArguments, ({ args, requestId }) => (
  <FilterRule rule={args} requestId={requestId} />
));

function FilterView({ result, context }: ResultPreviewProps<z.infer<typeof filterResult>>): JSX.Element {
  return (
    <Stack gap="xs">
      <FilterRule rule={result} requestId={context?.requestId} />
      <Text size="sm">
        <GmailLink href={filterSettings}>Open Gmail filters</GmailLink>
      </Text>
    </Stack>
  );
}

type FilterID = { id: string };
function FilterTarget({ id, requestId }: { id: string; requestId?: string }): JSX.Element {
  const summaries = useGmailSummaries(requestId);
  const rule = filterResult.safeParse(summaries?.filters.find((filter) => filter.id === id));
  return rule.success ? (
    <FilterView result={rule.data} />
  ) : (
    <Text size="sm">
      Filter <Code>{id}</Code>
    </Text>
  );
}

const getFilterPreview = definePreview(idArguments, ({ args, requestId }: PreviewProps<FilterID>) => (
  <FilterTarget id={args.id} requestId={requestId} />
));
function GetFilterCall({
  args,
  result,
  requestId,
}: CallPreviewProps<FilterID, z.infer<typeof filterResult>>): JSX.Element {
  return result === undefined ? <FilterTarget id={args.id} requestId={requestId} /> : <FilterView result={result} />;
}
const filterDeleteArguments = z.strictObject({ filter_id: z.string().min(1) });
const filterDeletePreview = definePreview(filterDeleteArguments, ({ args, requestId }) => (
  <Stack gap="xs">
    <Text size="sm">Removes this rule for future mail.</Text>
    <FilterTarget id={args.filter_id} requestId={requestId} />
  </Stack>
));

const filtersResult = z.strictObject({ filter: z.array(filterResult).nullish() });
const filtersView = defineResultPreview(filtersResult, ({ result }) => (
  <Stack gap="xs">
    <Text size="sm" c="dimmed">
      {result.filter?.length
        ? `${result.filter.length} ${result.filter.length === 1 ? "filter" : "filters"}`
        : "No filters returned."}
    </Text>
    {result.filter?.map((rule) => (
      <FilterRule key={rule.id} rule={rule} />
    ))}
    <Text size="sm">
      <GmailLink href={filterSettings}>Open Gmail filters</GmailLink>
    </Text>
  </Stack>
));

export const filterRenderers: ReadonlyMap<string, ActionPresentation> = new Map<string, ActionPresentation>([
  [
    "filters_list",
    {
      fallbackLabel: "List Gmail filters",
      pane: { collapsed: null },
      details: { arguments: definePreview(emptyArguments, () => null), result: filtersView },
      compactCardExpands: expandsForRejectedArguments(emptyArguments),
      offersCardDecisionButtons: (args) => emptyArguments.safeParse(args).success,
    },
  ],
  [
    "filters_get",
    {
      fallbackLabel: "Read Gmail filter",
      pane: { collapsed: null, opened: getFilterPreview },
      call: defineCallPreview(idArguments, filterResult, GetFilterCall),
      details: { arguments: getFilterPreview, result: defineResultPreview(filterResult, FilterView) },
    },
  ],
  [
    "filters_create",
    {
      fallbackLabel: "Create Gmail filter",
      pane: {
        collapsed: definePreview(filterCreateArguments, ({ args }) => (
          <Text size="sm">{args.action.forward ? "Forwards matching mail" : "Changes labels on matching mail"}</Text>
        )),
        opened: filterCreatePreview,
      },
      details: {
        arguments: filterCreatePreview,
        result: defineResultPreview(filterResult, ({ result, context }) => (
          <Stack gap="xs">
            <Text size="sm">Gmail created the filter.</Text>
            <FilterView result={result} context={context} />
          </Stack>
        )),
      },
    },
  ],
  [
    "filters_delete",
    {
      fallbackLabel: "Delete Gmail filter",
      pane: {
        collapsed: definePreview(filterDeleteArguments, () => <Text size="sm">Removes a rule for future mail.</Text>),
        opened: filterDeletePreview,
      },
      details: { arguments: filterDeletePreview, emptyResult: <Text size="sm">Filter deleted.</Text> },
    },
  ],
]);
