import { Anchor, Group, Stack, Text } from "@mantine/core";
import type { JSX, ReactNode } from "react";
import { z } from "zod";

import { defineResultPreview, type ResultPreview, type ResultPreviewProps } from "../result_entry";
import { zCreatePullRequestArguments } from "../../schemas/github/create_pull_request";
import { zUpdatePullRequestArguments } from "../../schemas/github/update_pull_request";

const repository = z.object({
  owner: z.string().regex(/^[\w-]+$/),
  repo: z
    .string()
    .regex(/^[\w.-]+$/)
    .refine((repo) => repo !== "." && repo !== ".."),
});
type Repository = z.infer<typeof repository>;

export function repositoryHref(repo: Repository): string {
  return `https://github.com/${encodeURIComponent(repo.owner)}/${encodeURIComponent(repo.repo)}`;
}

export function githubHref(value?: string | null): string | undefined {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    if (url.origin === "https://github.com" && !url.username && !url.password) return url.href;
  } catch {
    // Ignore malformed provider URLs.
  }
  return undefined;
}

export function GitHubLink({ href, children }: { href?: string; children: ReactNode }): JSX.Element {
  const valid = githubHref(href);
  return valid ? (
    <Anchor href={valid} target="_blank" rel="noreferrer" inherit>
      {children}
    </Anchor>
  ) : (
    <>{children}</>
  );
}

export function pullRequestLink(args: Repository & { pullNumber: number }): string {
  return `${repositoryHref(args)}/pull/${args.pullNumber}`;
}

const sha = z.string().regex(/^[a-f0-9]{7,64}$/i);
const referenceResult = z.object({
  ref: z
    .string()
    .regex(/^refs\/heads\/.+/)
    .refine((ref) => ref.split("/").every((segment) => segment !== "" && segment !== "." && segment !== "..")),
  object: z.object({ sha, type: z.literal("commit") }),
  url: z.string().optional(),
});

function refRepository(result: z.infer<typeof referenceResult>, args: unknown): Repository | undefined {
  const parsed = repository.safeParse(args);
  if (parsed.success) return parsed.data;
  if (!result.url) return undefined;
  try {
    const url = new URL(result.url);
    if (url.origin !== "https://api.github.com") return undefined;
    const [, owner, repo] = /^\/repos\/([^/]+)\/([^/]+)\/git\/refs\/heads\//.exec(url.pathname) ?? [];
    const parsed = repository.safeParse({ owner, repo });
    return parsed.success ? parsed.data : undefined;
  } catch {
    return undefined;
  }
}

function refPath(branch: string): string {
  return branch.split("/").map(encodeURIComponent).join("/");
}

function referenceReceipt(verb: string): ResultPreview {
  return defineResultPreview(referenceResult, ({ result, context }) => {
    const branch = result.ref.slice("refs/heads/".length);
    const repo = refRepository(result, context?.arguments);
    const base = repo ? repositoryHref(repo) : undefined;
    const commit = result.object.sha;
    return (
      <Stack gap={4}>
        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
          {verb} <GitHubLink href={base ? `${base}/tree/${refPath(branch)}` : undefined}>{branch}</GitHubLink>.
        </Text>
        <Text size="sm">
          Commit <GitHubLink href={base ? `${base}/commit/${commit}` : undefined}>{commit.slice(0, 7)}</GitHubLink>
        </Text>
      </Stack>
    );
  });
}

export const branchCreated: ResultPreview = referenceReceipt("Created branch");
export const filesPushed: ResultPreview = referenceReceipt("Pushed to");

const fileResult = z.object({
  commit: z.object({
    sha,
    html_url: z.string().nullish(),
  }),
  content: z
    .object({
      path: z.string().min(1),
      html_url: z.string().nullish(),
    })
    .nullish(),
});

function fileReceipt(deleted: boolean): ResultPreview {
  return defineResultPreview(fileResult, ({ result, context }) => {
    const repo = repository.safeParse(context?.arguments);
    const href =
      githubHref(result.commit.html_url) ??
      (repo.success ? `${repositoryHref(repo.data)}/commit/${result.commit.sha}` : undefined);
    return (
      <Stack gap={4}>
        <Text size="sm">{deleted ? "Deleted file." : "Committed file changes."}</Text>
        <Text size="sm">
          Commit <GitHubLink href={href}>{result.commit.sha.slice(0, 7)}</GitHubLink>
        </Text>
        {!deleted && result.content && (
          <Text size="sm" style={{ overflowWrap: "anywhere" }}>
            <GitHubLink href={githubHref(result.content.html_url)}>{result.content.path}</GitHubLink>
          </Text>
        )}
      </Stack>
    );
  });
}

export const fileWritten: ResultPreview = fileReceipt(false);
export const fileDeleted: ResultPreview = fileReceipt(true);

const awaiting = z.object({ status: z.literal("awaiting_user_submission"), reason: z.string().optional() });
const pullRequest = z
  .object({
    id: z.union([z.string().min(1), z.int().positive()]).optional(),
    number: z.int().positive().optional(),
    html_url: z.string().optional(),
    title: z.string().optional(),
    state: z.enum(["open", "closed"]).optional(),
    draft: z.boolean().optional(),
    merged: z.boolean().optional(),
  })
  .refine((pr) => pr.id !== undefined || pr.number !== undefined || pr.html_url !== undefined);
const pullRequestWriteResult = z.union([awaiting, pullRequest]);

export function awaitingGitHubSubmission(value: unknown): { label: string; failed: boolean } | undefined {
  return awaiting.safeParse(value).success ? { label: "Awaiting GitHub submission", failed: false } : undefined;
}

function pullRequestNumber(href: string | undefined): number | undefined {
  const match = href ? /\/pull\/(\d+)\/?$/.exec(new URL(href).pathname) : null;
  return match ? Number(match[1]) : undefined;
}

function pullRequestState(pr: { state?: "open" | "closed"; draft?: boolean; merged?: boolean }): string | undefined {
  if (pr.merged) return "Merged";
  if (pr.state === "closed") return "Closed";
  if (pr.draft) return "Draft";
  return pr.state === "open" ? "Open" : undefined;
}

function pullRequestWriteReceipt(created: boolean): ResultPreview {
  return defineResultPreview(pullRequestWriteResult, ({ result, context }) => {
    if ("status" in result) return <Text size="sm">GitHub is waiting for the form to be submitted.</Text>;
    const create = zCreatePullRequestArguments.safeParse(context?.arguments);
    const update = zUpdatePullRequestArguments.safeParse(context?.arguments);
    const href = githubHref(result.html_url) ?? (update.success ? pullRequestLink(update.data) : undefined);
    const number = result.number ?? pullRequestNumber(href);
    const title = result.title || (create.success ? create.data.title : update.success ? update.data.title : undefined);
    const status = pullRequestState(result);
    return (
      <Stack gap={4}>
        <Text size="sm">{created ? "Pull request created." : "Pull request updated."}</Text>
        {(href || title || number !== undefined) && (
          <Group gap="xs">
            <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
              <GitHubLink href={href}>
                {title || (number === undefined ? "Open pull request in GitHub" : `PR #${number}`)}
              </GitHubLink>
            </Text>
            {status && (
              <Text size="sm" c="dimmed">
                {status}
              </Text>
            )}
          </Group>
        )}
      </Stack>
    );
  });
}

export const pullRequestCreated: ResultPreview = pullRequestWriteReceipt(true);
export const pullRequestUpdated: ResultPreview = pullRequestWriteReceipt(false);

const inProgress = "Pull request branch update is in progress";
const branchUpdate = z.union([
  z.literal(inProgress),
  z.object({ message: z.string().min(1) }),
  z.strictObject({}),
  z.null(),
]);

export function githubBranchUpdateOutcome(value: unknown): { label: string; failed: boolean } | undefined {
  return branchUpdate.safeParse(value).success ? { label: "Update queued", failed: false } : undefined;
}

export const branchUpdateQueued: ResultPreview = defineResultPreview(branchUpdate, () => (
  <Text size="sm">GitHub accepted the update and is updating the pull request branch.</Text>
));

const pullRequestSearch = z.object({
  total_count: z.int().nonnegative(),
  incomplete_results: z.boolean().optional(),
  items: z.array(
    z
      .object({
        number: z.int().positive().optional(),
        title: z.string().optional(),
        state: z.enum(["open", "closed"]).optional(),
        html_url: z.string().optional(),
        repository_url: z.string().optional(),
        draft: z.boolean().optional(),
      })
      .refine((pr) => Boolean(pr.title || pr.number || pr.html_url))
  ),
});

function pullRequestRepository(pr: { html_url?: string; repository_url?: string }): string | undefined {
  const href = githubHref(pr.html_url);
  if (href) return /^\/([^/]+\/[^/]+)\/pull\//.exec(new URL(href).pathname)?.[1];
  if (!pr.repository_url) return undefined;
  return /^https:\/\/api\.github\.com\/repos\/([\w-]+\/[\w.-]+)\/?$/.exec(pr.repository_url)?.[1];
}

function PullRequestSearchReceipt({ result }: ResultPreviewProps<z.infer<typeof pullRequestSearch>>): JSX.Element {
  return (
    <Stack gap="xs">
      <Text size="sm" c="dimmed">
        {result.items.length === 0
          ? "No pull requests on this page."
          : `${result.items.length} of ${result.total_count} matching pull requests`}
      </Text>
      {result.incomplete_results && <Text size="sm">GitHub returned incomplete search results.</Text>}
      {result.items.map((pr, index) => {
        const ref = pr.number === undefined ? undefined : `#${pr.number}`;
        const target = pullRequestRepository(pr);
        const state = pullRequestState(pr);
        return (
          <Stack key={pr.html_url ?? index} gap={2}>
            <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
              <GitHubLink href={pr.html_url}>{pr.title || ref || "Pull request"}</GitHubLink>
            </Text>
            {(target || state) && (
              <Text size="xs" c="dimmed">
                {[target, state].filter(Boolean).join(" · ")}
              </Text>
            )}
          </Stack>
        );
      })}
    </Stack>
  );
}

export const pullRequestSearchResults: ResultPreview = defineResultPreview(pullRequestSearch, PullRequestSearchReceipt);
