import { Stack, Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import { TextBlock } from "../../../ui/text_block";
import type { ActionOutcome, ActionPresentation } from "../presentation";
import { defineResultPreview, type ResultPreviewProps } from "../result_entry";
import {
  displayState,
  Field,
  githubPresentation,
  GitHubItem,
  pageInput,
  RepoSlug,
  RepoTarget,
  repoInput,
  ResultRows,
} from "./display";
import { GitHubLink, githubHref, repositoryHref } from "./receipts";

const pagedRepo = repoInput.extend(pageInput);
const releaseTag = repoInput.extend({ tag: z.string().min(1) });
const release = z
  .object({
    tag_name: z.string().optional(),
    name: z.string().optional(),
    body: z.string().nullish(),
    html_url: z.string().optional(),
    published_at: z.string().nullish(),
    draft: z.boolean().optional(),
    prerelease: z.boolean().optional(),
    assets: z
      .array(z.object({ name: z.string(), browser_download_url: z.string().optional(), size: z.int().optional() }))
      .optional(),
  })
  .refine((release) => Boolean(release.tag_name || release.name || release.html_url));
type Release = z.infer<typeof release>;

function ReleaseView({ result }: ResultPreviewProps<Release>): JSX.Element {
  return (
    <GitHubItem
      title={result.name || result.tag_name || "Release"}
      href={result.html_url}
      info={[
        result.name !== result.tag_name ? result.tag_name : undefined,
        result.draft ? "Draft" : result.prerelease ? "Prerelease" : undefined,
        result.published_at,
      ]}
    >
      {result.body && <TextBlock text={result.body} heading="Release notes" format="prose" />}
      {result.assets?.map((asset) => (
        <Field
          key={asset.name}
          name="Asset"
          value={<GitHubLink href={asset.browser_download_url}>{asset.name}</GitHubLink>}
        />
      ))}
    </GitHubItem>
  );
}

const commitResult = z
  .object({
    sha: z.string().optional(),
    html_url: z.string().optional(),
    commit: z
      .object({
        message: z.string().optional(),
        author: z.object({ name: z.string().optional(), date: z.string().optional() }).nullish(),
      })
      .nullish(),
    stats: z.object({ additions: z.int().optional(), deletions: z.int().optional() }).nullish(),
    files: z
      .array(
        z.object({
          filename: z.string(),
          status: z.string().optional(),
          patch: z.string().optional(),
          additions: z.int().optional(),
          deletions: z.int().optional(),
        })
      )
      .nullish(),
    repository: z.object({ full_name: z.string().optional() }).nullish(),
  })
  .refine((result) => Boolean(result.sha || result.html_url || result.commit?.message));
type Commit = z.infer<typeof commitResult>;

function CommitView({ result, detail = false }: { result: Commit; detail?: boolean }): JSX.Element {
  const [subject, ...body] = result.commit?.message?.split("\n") ?? [];
  return (
    <GitHubItem
      title={subject || result.sha?.slice(0, 7) || "Commit"}
      href={result.html_url}
      info={[result.repository?.full_name, result.commit?.author?.name, result.commit?.author?.date]}
    >
      {detail && body.join("\n").trim() && (
        <TextBlock text={body.join("\n").trim()} heading="Commit message" format="prose" />
      )}
      {detail && result.stats && <Field name="Lines changed" value={counts(result.stats)} />}
      {detail &&
        result.files?.map((file) => (
          <GitHubItem key={file.filename} title={file.filename} info={[displayState(file.status), counts(file)]}>
            {file.patch && <TextBlock text={file.patch} heading="Patch" />}
          </GitHubItem>
        ))}
    </GitHubItem>
  );
}

function counts(value: { additions?: number; deletions?: number }): string {
  return [
    value.additions === undefined ? undefined : `+${value.additions}`,
    value.deletions === undefined ? undefined : `−${value.deletions}`,
  ]
    .filter(Boolean)
    .join(" · ");
}

const commitGet = pagedRepo.extend({ sha: z.string(), detail: z.enum(["none", "stats", "full_patch"]).optional() });
const commitList = pagedRepo.extend({
  sha: z.string().optional(),
  author: z.string().optional(),
  path: z.string().optional(),
  fields: z.array(z.string()).optional(),
  since: z.string().optional(),
  until: z.string().optional(),
});
const repoCreate = z.strictObject({
  name: z.string().min(1),
  description: z.string().optional(),
  organization: z.string().optional(),
  private: z.boolean().optional(),
  autoInit: z.boolean().optional(),
});
const repoMutation = z.object({
  id: z.union([z.string(), z.int()]),
  url: z.string().optional(),
  html_url: z.string().optional(),
  full_name: z.string().optional(),
});
const forkArgs = repoInput.extend({ organization: z.string().optional() });
const forkQueued = z.union([z.literal("Fork is in progress"), z.object({ message: z.literal("Fork is in progress") })]);
const forkResult = z.union([forkQueued, repoMutation]);
function forkOutcome(value: unknown): ActionOutcome | undefined {
  return forkResult.safeParse(value).success ? { label: "Fork accepted", failed: false } : undefined;
}

const fileArgs = repoInput.extend({
  ref: z.string().optional(),
  sha: z.string().optional(),
  fields: z.array(z.string()).optional(),
  path: z.string().optional(),
});
const directory = z.array(
  z
    .object({
      name: z.string().optional(),
      path: z.string().optional(),
      type: z.string().optional(),
      size: z.int().optional(),
      html_url: z.string().optional(),
    })
    .refine((entry) => Boolean(entry.name || entry.path || entry.html_url))
);
const fileContent = z.object({
  content: z.array(
    z.discriminatedUnion("type", [
      z.object({ type: z.literal("text"), text: z.string() }),
      z.object({ type: z.literal("resource"), resource: z.object({ text: z.string(), uri: z.string() }) }),
      z.object({ type: z.literal("resource_link"), uri: z.string(), name: z.string(), title: z.string().optional() }),
    ])
  ),
});
const branchList = z.array(z.object({ name: z.string(), protected: z.boolean().optional() })).nullable();
const tagList = z.array(z.object({ name: z.string() })).nullable();
const tagResult = z.union([
  z.object({ ref: z.string().regex(/^refs\/tags\/.+/), object: z.object({ sha: z.string(), type: z.string() }) }),
  z.object({
    tag: z.string().min(1),
    sha: z.string(),
    message: z.string().optional(),
    tagger: z.object({ name: z.string().optional() }).nullish(),
  }),
]);
const releaseList = pagedRepo.extend({ fields: z.array(z.string()).optional() });
const searchCommits = z.strictObject({
  ...pageInput,
  query: z.string(),
  sort: z.enum(["author-date", "committer-date"]).optional(),
  order: z.enum(["asc", "desc"]).optional(),
});

function refHref(repo: z.infer<typeof repoInput>, mode: "tree" | "releases/tag", ref: string): string | undefined {
  const segments = ref.split("/");
  if (segments.some((part) => part === "" || part === "." || part === "..")) return undefined;
  return `${repositoryHref(repo)}/${mode}/${segments.map(encodeURIComponent).join("/")}`;
}
const commitDetail = { none: "Summary only", stats: "Includes change counts", full_patch: "Includes file patches" };

export const repositoryRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  [
    "create_repository",
    githubPresentation({
      label: "Create GitHub repository",
      schema: repoCreate,
      compact: ({ args }) => <Text size="sm">{args.name}</Text>,
      request: ({ args }) => (
        <Stack gap={4}>
          <Field name="Name" value={args.name} />
          <Field name="Owner" value={args.organization || "Authenticated GitHub account"} />
          <Field name="Visibility" value={args.private === false ? "Public" : "Private"} />
          {args.autoInit && <Text size="sm">Initialize with a README.</Text>}
          {args.description && <TextBlock text={args.description} heading="Description" format="prose" />}
        </Stack>
      ),
      result: defineResultPreview(repoMutation, ({ result, context }) => {
        const args = repoCreate.safeParse(context?.arguments);
        return (
          <GitHubItem
            title={result.full_name || (args.success ? args.data.name : "Repository created")}
            href={githubHref(result.html_url) ?? githubHref(result.url)}
            info={["Repository created"]}
          />
        );
      }),
    }),
  ],
  [
    "fork_repository",
    githubPresentation({
      label: "Fork GitHub repository",
      schema: forkArgs,
      outcome: forkOutcome,
      compact: RepoSlug,
      request: ({ args }) => (
        <Stack gap={4}>
          <RepoTarget repo={args} />
          <Field name="Destination owner" value={args.organization || "Authenticated GitHub account"} />
        </Stack>
      ),
      result: defineResultPreview(forkResult, ({ result }) => {
        if (typeof result === "string" || "message" in result)
          return <Text size="sm">GitHub accepted the fork and is creating it.</Text>;
        return (
          <GitHubItem
            title={result.full_name || "Open fork in GitHub"}
            href={githubHref(result.html_url) ?? githubHref(result.url)}
            info={["Fork accepted"]}
          />
        );
      }),
    }),
  ],
  [
    "get_file_contents",
    githubPresentation({
      label: "Read GitHub repository contents",
      schema: fileArgs,
      cardDecision: true,
      compact: ({ args }) => (
        <Text size="sm">
          {args.owner}/{args.repo} · {args.path || "Repository root"}
        </Text>
      ),
      request: ({ args }) => (
        <Stack gap={4}>
          <RepoTarget repo={args} />
          <Field name="Path" value={args.path || "Repository root"} />
          <Field name="Ref" value={args.ref || "Default branch"} />
          <Field name="Commit" value={args.sha} />
        </Stack>
      ),
      result: defineResultPreview(z.union([directory, fileContent]), ({ result }) => {
        if (Array.isArray(result))
          return (
            <ResultRows count={result.length} name="entries">
              {result.map((entry, index) => (
                <GitHubItem
                  key={entry.path ?? index}
                  title={entry.path || entry.name || "Repository entry"}
                  href={entry.html_url}
                  info={[displayState(entry.type), entry.size === undefined ? undefined : `${entry.size} bytes`]}
                />
              ))}
            </ResultRows>
          );
        return (
          <Stack gap="xs">
            {result.content.map((block, index) => {
              if (block.type === "text")
                return (
                  <Text key={index} size="sm">
                    {block.text}
                  </Text>
                );
              if (block.type === "resource")
                return <TextBlock key={index} text={block.resource.text} heading="File contents" />;
              return <GitHubItem key={index} title={block.title || block.name} href={block.uri} />;
            })}
          </Stack>
        );
      }),
    }),
  ],
  [
    "get_commit",
    githubPresentation({
      label: "Read GitHub commit",
      schema: commitGet,
      cardDecision: true,
      compact: ({ args }) => (
        <Text size="sm">
          {args.owner}/{args.repo} · {args.sha}
        </Text>
      ),
      request: ({ args }) => (
        <Stack gap={4}>
          <RepoTarget repo={args} />
          <Field name="Commit or ref" value={args.sha} />
          <Text size="sm">{commitDetail[args.detail ?? "stats"]}</Text>
          <Field name="Files per page" value={args.perPage} />
        </Stack>
      ),
      result: defineResultPreview(commitResult, ({ result }) => <CommitView result={result} detail />),
    }),
  ],
  [
    "list_commits",
    githubPresentation({
      label: "List GitHub commits",
      cardDecision: true,
      schema: commitList,
      compact: RepoSlug,
      request: ({ args }) => (
        <Stack gap={4}>
          <RepoTarget repo={args} />
          <Field name="Ref" value={args.sha || "Default branch"} />
          <Field name="Path" value={args.path} />
          <Field name="Author" value={args.author} />
          <Field name="Since" value={args.since} />
          <Field name="Until" value={args.until} />
          <Field name="Page size" value={args.perPage} />
        </Stack>
      ),
      result: defineResultPreview(z.array(commitResult).nullable(), ({ result }) => (
        <ResultRows count={result?.length ?? 0} name="commits">
          {result?.map((commit, index) => (
            <CommitView key={commit.sha ?? index} result={commit} />
          ))}
        </ResultRows>
      )),
    }),
  ],
  [
    "list_branches",
    githubPresentation({
      label: "List GitHub branches",
      schema: pagedRepo,
      cardDecision: true,
      compact: RepoSlug,
      request: ({ args }) => (
        <Stack gap={4}>
          <RepoTarget repo={args} path="/branches" />
          <Field name="Page size" value={args.perPage} />
        </Stack>
      ),
      result: defineResultPreview(branchList, ({ result, context }) => {
        const args = pagedRepo.safeParse(context?.arguments);
        return (
          <ResultRows count={result?.length ?? 0} name="branches">
            {result?.map((branch) => (
              <GitHubItem
                key={branch.name}
                title={branch.name}
                href={args.success ? refHref(args.data, "tree", branch.name) : undefined}
                info={[branch.protected ? "Protected" : undefined]}
              />
            ))}
          </ResultRows>
        );
      }),
    }),
  ],
  [
    "list_tags",
    githubPresentation({
      label: "List GitHub tags",
      cardDecision: true,
      schema: pagedRepo,
      compact: RepoSlug,
      request: ({ args }) => (
        <Stack gap={4}>
          <RepoTarget repo={args} path="/tags" />
          <Field name="Page size" value={args.perPage} />
        </Stack>
      ),
      result: defineResultPreview(tagList, ({ result, context }) => {
        const args = pagedRepo.safeParse(context?.arguments);
        return (
          <ResultRows count={result?.length ?? 0} name="tags">
            {result?.map((tag) => (
              <GitHubItem
                key={tag.name}
                title={tag.name}
                href={args.success ? refHref(args.data, "tree", tag.name) : undefined}
              />
            ))}
          </ResultRows>
        );
      }),
    }),
  ],
  [
    "get_tag",
    githubPresentation({
      label: "Read GitHub tag",
      schema: releaseTag,
      cardDecision: true,
      compact: ({ args }) => (
        <Text size="sm">
          {args.owner}/{args.repo} · {args.tag}
        </Text>
      ),
      request: ({ args }) => (
        <RepoTarget repo={args} path={`/tree/${encodeURIComponent(args.tag)}`}>
          {args.owner}/{args.repo} · {args.tag}
        </RepoTarget>
      ),
      result: defineResultPreview(tagResult, ({ result, context }) => {
        const args = releaseTag.safeParse(context?.arguments);
        const name = "tag" in result ? result.tag : result.ref.slice("refs/tags/".length);
        return (
          <GitHubItem
            title={name}
            href={args.success ? refHref(args.data, "tree", name) : undefined}
            info={["tag" in result ? "Annotated tag" : "Lightweight tag"]}
          >
            {"tag" in result && result.message && (
              <TextBlock text={result.message} heading="Tag message" format="prose" />
            )}
          </GitHubItem>
        );
      }),
    }),
  ],
  [
    "list_releases",
    githubPresentation({
      label: "List GitHub releases",
      cardDecision: true,
      schema: releaseList,
      compact: RepoSlug,
      request: ({ args }) => (
        <Stack gap={4}>
          <RepoTarget repo={args} path="/releases" />
          <Field name="Page size" value={args.perPage} />
        </Stack>
      ),
      result: defineResultPreview(z.array(release).nullable(), ({ result }) => (
        <ResultRows count={result?.length ?? 0} name="releases">
          {result?.map((release, index) => (
            <ReleaseView key={release.html_url ?? index} result={release} />
          ))}
        </ResultRows>
      )),
    }),
  ],
  [
    "get_latest_release",
    githubPresentation({
      label: "Read latest GitHub release",
      cardDecision: true,
      schema: repoInput,
      compact: RepoSlug,
      request: ({ args }) => <RepoTarget repo={args} path="/releases/latest" />,
      result: defineResultPreview(release, ReleaseView),
    }),
  ],
  [
    "get_release_by_tag",
    githubPresentation({
      label: "Read GitHub release by tag",
      schema: releaseTag,
      cardDecision: true,
      compact: ({ args }) => (
        <Text size="sm">
          {args.owner}/{args.repo} · {args.tag}
        </Text>
      ),
      request: ({ args }) => (
        <Stack gap={4}>
          <RepoTarget repo={args} path="/releases" />
          <Field name="Tag" value={args.tag} />
        </Stack>
      ),
      result: defineResultPreview(release, ReleaseView),
    }),
  ],
  [
    "search_commits",
    githubPresentation({
      label: "Search GitHub commits",
      schema: searchCommits,
      compact: ({ args }) => <Field name="Query" value={args.query} />,
      request: ({ args }) => (
        <Stack gap={4}>
          <Field name="Query" value={args.query} />
          <Field name="Page" value={args.page} />
          <Field name="Page size" value={args.perPage} />
        </Stack>
      ),
      staticCard: true,
      hasFoldedArguments: (args) => args.page !== undefined || args.perPage !== undefined,
      result: defineResultPreview(
        z.object({
          items: z.array(commitResult),
          total_count: z.int().optional(),
          incomplete_results: z.boolean().optional(),
        }),
        ({ result }) => (
          <ResultRows
            count={result.items.length}
            total={result.total_count}
            incomplete={result.incomplete_results}
            name="commits"
          >
            {result.items.map((commit, index) => (
              <CommitView key={commit.sha ?? index} result={commit} />
            ))}
          </ResultRows>
        )
      ),
    }),
  ],
]);
