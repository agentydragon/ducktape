import { Stack, Text } from "@mantine/core";
import type { JSX, ReactNode } from "react";
import { z } from "zod";

import { definePreview, type PreviewProps } from "../entry";
import { expandsForRejectedArguments, type ActionOutcome, type ActionPresentation } from "../presentation";
import { defineResultPreview, renderResultPreview, type ResultPreview } from "../result_entry";
import { GitHubLink, repositoryHref } from "./receipts";

export type RepoArgs = { owner: string; repo: string };
export const repoInput: z.ZodObject<{ owner: z.ZodString; repo: z.ZodString }> = z.strictObject({
  owner: z.string().min(1),
  repo: z.string().min(1),
});
export const pageInput: { page: z.ZodOptional<z.ZodNumber>; perPage: z.ZodOptional<z.ZodNumber> } = {
  page: z.int().positive().optional(),
  perPage: z.int().positive().optional(),
};
export const pagination: z.ZodObject<{ hasNextPage: z.ZodOptional<z.ZodBoolean> }> = z.object({
  hasNextPage: z.boolean().optional(),
});
export const account: z.ZodObject<{ login: z.ZodOptional<z.ZodString> }> = z.object({ login: z.string().optional() });

export function Field({ name, value }: { name: string; value?: ReactNode }): JSX.Element | null {
  if (value === null || value === undefined || value === "") return null;
  return (
    <Text size="sm" style={{ overflowWrap: "anywhere" }}>
      <strong>{name}</strong> {value}
    </Text>
  );
}

export function RepoTarget({
  repo,
  path = "",
  children,
}: {
  repo: RepoArgs;
  path?: string;
  children?: ReactNode;
}): JSX.Element {
  return (
    <Text size="sm" style={{ overflowWrap: "anywhere" }}>
      <GitHubLink href={`${repositoryHref(repo)}${path}`}>{children ?? `${repo.owner}/${repo.repo}`}</GitHubLink>
    </Text>
  );
}

export function ResultRows({
  children,
  count,
  name,
  total,
  more,
  incomplete,
}: {
  children: ReactNode;
  count: number;
  name: string;
  total?: number;
  more?: boolean;
  incomplete?: boolean;
}): JSX.Element {
  return (
    <Stack gap="xs">
      <Text size="sm" c="dimmed">
        {count === 0
          ? `No ${name} returned.`
          : `${count}${total !== undefined && total > count ? ` of ${total}` : ""} ${name}`}
      </Text>
      {children}
      {more && <Text size="sm">More results available.</Text>}
      {incomplete && <Text size="sm">GitHub returned an incomplete result.</Text>}
    </Stack>
  );
}

export function GitHubItem({
  title,
  href,
  info,
  children,
}: {
  title: string;
  href?: string;
  info?: Array<string | undefined | null>;
  children?: ReactNode;
}): JSX.Element {
  const summary = info?.filter(Boolean).join(" · ");
  return (
    <Stack gap={3}>
      <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
        <GitHubLink href={href}>{title}</GitHubLink>
      </Text>
      {summary && (
        <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
          {summary}
        </Text>
      )}
      {children}
    </Stack>
  );
}

export function githubPresentation<S extends z.ZodType>({
  label,
  schema,
  request,
  compact,
  staticCard = false,
  result,
  outcome,
}: {
  label: string;
  schema: S;
  request?: (props: PreviewProps<z.infer<S>>) => ReactNode;
  compact?: (props: PreviewProps<z.infer<S>>) => ReactNode;
  staticCard?: boolean;
  result: ResultPreview | null;
  outcome?: (value: unknown) => ActionOutcome | undefined;
}): ActionPresentation {
  const preview = request ? definePreview(schema, request) : { schema, render: () => null };
  const compactPreview = compact ? definePreview(schema, compact) : null;
  return {
    fallbackLabel: label,
    ...(outcome ? { outcome } : {}),
    pane: { collapsed: compactPreview, ...(staticCard ? {} : { opened: preview }) },
    details: { arguments: preview, result },
    ...(staticCard
      ? {
          compactCardExpands: expandsForRejectedArguments(schema),
          offersCardDecisionButtons: (args: unknown) => schema.safeParse(args).success,
        }
      : {}),
  };
}

export function byMethod(views: Record<string, ResultPreview>): ResultPreview {
  const input = z.object({ method: z.string() });
  const entries = new Map(Object.entries(views));
  return {
    schema: z.unknown(),
    render: (result, context) => {
      const method = input.safeParse(context?.arguments);
      const preview = method.success ? entries.get(method.data.method) : undefined;
      return preview ? renderResultPreview(preview, result, context) : undefined;
    },
  };
}

export function acknowledgement(text: string, structured: string = text): ResultPreview {
  const result = z.union([z.literal(text), z.object({ message: z.literal(structured) })]);
  return defineResultPreview(result, ({ result }) => (
    <Text size="sm">{typeof result === "string" ? result : result.message}</Text>
  ));
}

export function displayState(state?: string | null): string | undefined {
  if (!state) return undefined;
  const normalized = state.toLowerCase().replaceAll("_", " ");
  return normalized[0].toUpperCase() + normalized.slice(1);
}
