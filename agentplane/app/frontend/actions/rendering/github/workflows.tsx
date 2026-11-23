import { Stack, Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import { JsonBlock, TextBlock } from "../../../ui/text_block";
import type { ActionOutcome, ActionPresentation } from "../presentation";
import { defineResultPreview, type ResultPreviewProps } from "../result_entry";
import {
  account,
  byMethod,
  displayState,
  Field,
  githubPresentation,
  GitHubItem,
  pageInput,
  RepoTarget,
  repoInput,
  ResultRows,
  type RepoArgs,
} from "./display";
import { GitHubLink, repositoryHref } from "./receipts";

const getMethods = {
  get_workflow: "Workflow",
  get_workflow_run: "Workflow run",
  get_workflow_job: "Workflow job",
  download_workflow_run_artifact: "Artifact download",
  get_workflow_run_usage: "Run usage",
  get_workflow_run_logs_url: "Workflow run logs",
} as const;
const listMethods = {
  list_workflows: "Workflows",
  list_workflow_runs: "Workflow runs",
  list_workflow_jobs: "Workflow jobs",
  list_workflow_run_artifacts: "Run artifacts",
} as const;
const triggerMethods = {
  run_workflow: "Queue a workflow run",
  rerun_workflow_run: "Rerun all jobs",
  rerun_failed_jobs: "Rerun failed jobs only",
  cancel_workflow_run: "Request cancellation",
  delete_workflow_run_logs: "Delete run logs",
} as const;
const getArgs = repoInput.extend({
  method: z.enum(Object.keys(getMethods) as [keyof typeof getMethods, ...(keyof typeof getMethods)[]]),
  resource_id: z.string().min(1),
});
const listArgs = repoInput.extend({
  ...pageInput,
  method: z.enum(Object.keys(listMethods) as [keyof typeof listMethods, ...(keyof typeof listMethods)[]]),
  resource_id: z.string().optional(),
  workflow_runs_filter: z
    .strictObject({
      actor: z.string().optional(),
      branch: z.string().optional(),
      event: z.string().optional(),
      status: z.enum(["queued", "in_progress", "completed", "requested", "waiting"]).optional(),
    })
    .optional(),
  workflow_jobs_filter: z.strictObject({ filter: z.enum(["latest", "all"]).optional() }).optional(),
});
const triggerArgs = repoInput.extend({
  method: z.enum(Object.keys(triggerMethods) as [keyof typeof triggerMethods, ...(keyof typeof triggerMethods)[]]),
  inputs: z.record(z.string(), z.unknown()).optional(),
  ref: z.string().optional(),
  run_id: z.int().positive().optional(),
  workflow_id: z.string().optional(),
});
const logsArgs = repoInput.extend({
  job_id: z.int().positive().optional(),
  run_id: z.int().positive().optional(),
  failed_only: z.boolean().optional(),
  return_content: z.boolean().optional(),
  tail_lines: z.int().nonnegative().optional(),
});

function wrapped<S extends z.ZodType>(field: string, schema: S) {
  return z.preprocess((value) => {
    if (typeof value !== "object" || value === null || !(field in value)) return value;
    const item = (value as Record<string, unknown>)[field];
    return item !== null && typeof item === "object" && !Array.isArray(item) ? item : value;
  }, schema);
}

const workflow = z
  .object({
    id: z.int().optional(),
    name: z.string().optional(),
    path: z.string().optional(),
    state: z.string().optional(),
    html_url: z.string().optional(),
  })
  .refine((workflow) => Boolean(workflow.name || workflow.path || workflow.html_url || workflow.id));
const run = z.object({
  id: z.int(),
  name: z.string().optional(),
  display_title: z.string().optional(),
  status: z.string().optional(),
  conclusion: z.string().nullish(),
  head_branch: z.string().optional(),
  html_url: z.string().optional(),
  actor: account.nullish(),
  run_number: z.int().optional(),
  run_attempt: z.int().optional(),
  created_at: z.string().optional(),
});
const job = z.object({
  id: z.int(),
  run_id: z.int().optional(),
  name: z.string(),
  status: z.string().optional(),
  conclusion: z.string().nullish(),
  html_url: z.string().optional(),
  runner_name: z.string().optional(),
  steps: z
    .array(
      z.object({ name: z.string(), number: z.int(), status: z.string().optional(), conclusion: z.string().nullish() })
    )
    .optional(),
});
const artifact = z.object({
  id: z.int().optional(),
  name: z.string(),
  expired: z.boolean().optional(),
  size_in_bytes: z.int().optional(),
});
const artifactDownload = z.object({ artifact_id: z.int(), download_url: z.string(), note: z.string().optional() });
const logsDownload = z.object({ logs_url: z.string(), note: z.string().optional() });
const runUsage = z
  .object({
    run_duration_ms: z.int().optional(),
    billable: z
      .union([
        z.array(z.object({ runner: z.string(), total_ms: z.int().optional(), jobs: z.int().optional() })),
        z.record(z.string(), z.object({ total_ms: z.int().optional(), jobs: z.int().optional() })),
      ])
      .optional(),
  })
  .refine((value) => value.run_duration_ms !== undefined || value.billable !== undefined);

function runUrl(repo: RepoArgs, id: number | string): string | undefined {
  return /^\d+$/.test(String(id)) ? `${repositoryHref(repo)}/actions/runs/${id}` : undefined;
}

function Workflow({ result }: ResultPreviewProps<z.infer<typeof workflow>>): JSX.Element {
  return (
    <GitHubItem
      title={result.name || result.path || `Workflow ${result.id}`}
      href={result.html_url}
      info={[result.name ? result.path : undefined, displayState(result.state)]}
    />
  );
}

function Run({ result }: ResultPreviewProps<z.infer<typeof run>>): JSX.Element {
  return (
    <GitHubItem
      title={result.display_title || result.name || `Run ${result.id}`}
      href={result.html_url}
      info={[
        displayState(result.conclusion || result.status),
        result.head_branch,
        result.actor?.login,
        result.created_at,
        result.run_attempt === undefined ? undefined : `Attempt ${result.run_attempt}`,
      ]}
    />
  );
}

function Job({ result, detail = false }: ResultPreviewProps<z.infer<typeof job>> & { detail?: boolean }): JSX.Element {
  return (
    <GitHubItem
      title={result.name}
      href={result.html_url}
      info={[displayState(result.conclusion || result.status), result.runner_name]}
    >
      {detail &&
        result.steps?.map((step) => (
          <Field key={step.number} name={step.name} value={displayState(step.conclusion || step.status)} />
        ))}
    </GitHubItem>
  );
}

const getResult = byMethod({
  get_workflow: defineResultPreview(wrapped("workflow", workflow), Workflow),
  get_workflow_run: defineResultPreview(wrapped("workflow_run", run), Run),
  get_workflow_job: defineResultPreview(wrapped("workflow_job", job), ({ result }) => <Job result={result} detail />),
  get_workflow_run_usage: defineResultPreview(wrapped("usage", runUsage), ({ result }) => {
    const rows = Array.isArray(result.billable)
      ? result.billable
      : Object.entries(result.billable ?? {}).map(([runner, usage]) => ({ runner, ...usage }));
    return (
      <Stack gap={4}>
        <Field
          name="Run duration"
          value={result.run_duration_ms === undefined ? undefined : `${result.run_duration_ms} ms`}
        />
        <ResultRows count={rows.length} name="runner types">
          {rows.map((usage) => (
            <GitHubItem
              key={usage.runner}
              title={usage.runner}
              info={[
                usage.total_ms === undefined ? undefined : `${usage.total_ms} ms`,
                usage.jobs === undefined ? undefined : `${usage.jobs} jobs`,
              ]}
            />
          ))}
        </ResultRows>
      </Stack>
    );
  }),
  download_workflow_run_artifact: defineResultPreview(wrapped("artifact", artifactDownload), () => (
    <Text size="sm">GitHub returned a temporary artifact download URL. View it in Raw.</Text>
  )),
  get_workflow_run_logs_url: defineResultPreview(wrapped("logs", logsDownload), ({ context }) => {
    const args = getArgs.safeParse(context?.arguments);
    const href = args.success ? runUrl(args.data, args.data.resource_id) : undefined;
    return (
      <Stack gap={4}>
        <Text size="sm">GitHub returned a temporary logs URL. View it in Raw.</Text>
        {href && <GitHubItem title="Open workflow run" href={href} />}
      </Stack>
    );
  }),
});

const jobPage = z.object({ jobs: z.array(job), total_count: z.int().optional() });
const listResult = byMethod({
  list_workflows: defineResultPreview(
    wrapped("workflows", z.object({ workflows: z.array(workflow).nullish(), total_count: z.int() })),
    ({ result }) => (
      <ResultRows count={result.workflows?.length ?? 0} total={result.total_count} name="workflows">
        {result.workflows?.map((item, i) => (
          <Workflow key={item.id ?? i} result={item} />
        ))}
      </ResultRows>
    )
  ),
  list_workflow_runs: defineResultPreview(
    wrapped("workflow_runs", z.object({ workflow_runs: z.array(run), total_count: z.int().optional() })),
    ({ result }) => (
      <ResultRows count={result.workflow_runs.length} total={result.total_count} name="workflow runs">
        {result.workflow_runs.map((item) => (
          <Run key={item.id} result={item} />
        ))}
      </ResultRows>
    )
  ),
  list_workflow_jobs: defineResultPreview(wrapped("workflow_jobs", wrapped("jobs", jobPage)), ({ result }) => (
    <ResultRows count={result.jobs.length} total={result.total_count} name="jobs">
      {result.jobs.map((item) => (
        <Job key={item.id} result={item} />
      ))}
    </ResultRows>
  )),
  list_workflow_run_artifacts: defineResultPreview(
    wrapped("artifacts", z.object({ artifacts: z.array(artifact).nullish(), total_count: z.int() })),
    ({ result, context }) => {
      const args = listArgs.safeParse(context?.arguments);
      const href = args.success && args.data.resource_id ? runUrl(args.data, args.data.resource_id) : undefined;
      return (
        <ResultRows count={result.artifacts?.length ?? 0} total={result.total_count} name="artifacts">
          {result.artifacts?.map((artifact, index) => (
            <GitHubItem
              key={artifact.id ?? index}
              title={artifact.name}
              href={href && artifact.id && !artifact.expired ? `${href}/artifacts/${artifact.id}` : undefined}
              info={[
                artifact.expired ? "Expired" : undefined,
                artifact.size_in_bytes === undefined ? undefined : `${artifact.size_in_bytes} bytes`,
              ]}
            />
          ))}
        </ResultRows>
      );
    }
  ),
});

const dispatch = z.object({ workflow_id: z.string(), ref: z.string(), status_code: z.int(), message: z.string() });
const operation = z.object({ run_id: z.int(), status_code: z.int(), message: z.string() });
const triggerFields = {
  run_workflow: "dispatch",
  rerun_workflow_run: "rerun",
  rerun_failed_jobs: "rerun_failed",
  cancel_workflow_run: "cancel",
  delete_workflow_run_logs: "delete_logs",
} as const;

function statusLabel(method: keyof typeof triggerMethods, status: number): string {
  if (status < 200 || status >= 300) return "GitHub rejected the operation";
  if (method === "cancel_workflow_run") return "Cancellation requested";
  if (method === "delete_workflow_run_logs") return status === 202 ? "Log deletion requested" : "Run logs deleted";
  return method === "run_workflow" ? "Workflow run queued" : "Rerun queued";
}

function runOperation(method: Exclude<keyof typeof triggerMethods, "run_workflow">) {
  return defineResultPreview(wrapped(triggerFields[method], operation), ({ result, context }) => {
    const args = triggerArgs.safeParse(context?.arguments);
    return (
      <Stack gap={4}>
        <Text size="sm">{statusLabel(method, result.status_code)}</Text>
        {args.success && <GitHubItem title="Open workflow run" href={runUrl(args.data, result.run_id)} />}
      </Stack>
    );
  });
}
const triggerResult = byMethod({
  run_workflow: defineResultPreview(wrapped("dispatch", dispatch), ({ result, context }) => {
    const args = triggerArgs.safeParse(context?.arguments);
    return (
      <Stack gap={4}>
        <Text size="sm">{statusLabel("run_workflow", result.status_code)}</Text>
        {args.success && (
          <RepoTarget repo={args.data} path="/actions">
            Open repository Actions
          </RepoTarget>
        )}
      </Stack>
    );
  }),
  rerun_workflow_run: runOperation("rerun_workflow_run"),
  rerun_failed_jobs: runOperation("rerun_failed_jobs"),
  cancel_workflow_run: runOperation("cancel_workflow_run"),
  delete_workflow_run_logs: runOperation("delete_workflow_run_logs"),
});

const outcomeSchema = z.object({ message: z.string(), status_code: z.int() });
const legacyTriggerMethods: ReadonlyMap<string, keyof typeof triggerMethods> = new Map([
  ["Workflow run has been queued", "run_workflow"],
  ["Workflow run has been queued for re-run", "rerun_workflow_run"],
  ["Failed jobs have been queued for re-run", "rerun_failed_jobs"],
  ["Workflow run has been cancelled", "cancel_workflow_run"],
  ["Workflow run logs have been deleted", "delete_workflow_run_logs"],
]);
function triggerOutcome(value: unknown): ActionOutcome | undefined {
  const modern = triggerArgs.pick({ method: true }).loose().safeParse(value);
  const legacy = outcomeSchema.safeParse(value);
  const method = modern.success
    ? modern.data.method
    : legacy.success
      ? legacyTriggerMethods.get(legacy.data.message)
      : undefined;
  if (!method) return undefined;
  const result = wrapped(triggerFields[method], outcomeSchema).safeParse(value);
  if (!result.success) return undefined;
  const status = result.data.status_code;
  return { label: statusLabel(method, status), failed: status < 200 || status >= 300 };
}

const jobLog = z
  .object({
    job_id: z.int(),
    job_name: z.string().optional(),
    logs_content: z.string().optional(),
    logs_url: z.string().optional(),
    original_length: z.int().nonnegative().optional(),
    error: z.string().optional(),
  })
  .refine((log) => log.logs_content !== undefined || Boolean(log.logs_url || log.error));
const failedLogs = z.object({ failed_jobs: z.int(), total_jobs: z.int().optional(), logs: z.array(jobLog).optional() });

function JobLog({ log, repo, run }: { log: z.infer<typeof jobLog>; repo?: RepoArgs; run?: number }): JSX.Element {
  const href = repo && run ? `${runUrl(repo, run)}/job/${log.job_id}` : undefined;
  return (
    <GitHubItem title={log.job_name || `Job ${log.job_id}`} href={href}>
      {log.error && (
        <Text size="sm" c="red">
          {log.error}
        </Text>
      )}
      {log.logs_content !== undefined && <TextBlock text={log.logs_content} heading="Job log" />}
      {log.logs_content !== undefined &&
        log.original_length !== undefined &&
        new TextEncoder().encode(log.logs_content).length < log.original_length && (
          <Text size="sm">GitHub returned a shortened log.</Text>
        )}
      {log.logs_url && <Text size="sm">GitHub returned a temporary download URL. View it in Raw.</Text>}
    </GitHubItem>
  );
}

export const workflowRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  [
    "actions_get",
    githubPresentation({
      label: "Read GitHub Actions data",
      schema: getArgs,
      result: getResult,
      compact: ({ args }) => <Text size="sm">{getMethods[args.method]}</Text>,
      request: ({ args }) => (
        <Stack gap={4}>
          <RepoTarget repo={args} path="/actions" />
          <Field name={getMethods[args.method]} value={args.resource_id} />
        </Stack>
      ),
    }),
  ],
  [
    "actions_list",
    githubPresentation({
      label: "List GitHub Actions data",
      schema: listArgs,
      result: listResult,
      compact: ({ args }) => <Text size="sm">{listMethods[args.method]}</Text>,
      request: ({ args }) => (
        <Stack gap={4}>
          <RepoTarget repo={args} path="/actions" />
          <Field name="List" value={listMethods[args.method]} />
          <Field name="Workflow or run" value={args.resource_id} />
          <Field name="Actor" value={args.workflow_runs_filter?.actor} />
          <Field name="Branch" value={args.workflow_runs_filter?.branch} />
          <Field name="Event" value={displayState(args.workflow_runs_filter?.event)} />
          <Field name="Status" value={displayState(args.workflow_runs_filter?.status)} />
          <Field name="Job attempts" value={displayState(args.workflow_jobs_filter?.filter)} />
          <Field name="Page size" value={args.perPage} />
        </Stack>
      ),
    }),
  ],
  [
    "actions_run_trigger",
    githubPresentation({
      label: "Change GitHub workflow run",
      schema: triggerArgs,
      result: triggerResult,
      outcome: triggerOutcome,
      compact: ({ args }) => <Text size="sm">{triggerMethods[args.method]}</Text>,
      request: ({ args }) => (
        <Stack gap={4}>
          <RepoTarget repo={args} path="/actions" />
          <Field name="Operation" value={triggerMethods[args.method]} />
          {args.run_id && (
            <Field name="Run" value={<GitHubLink href={runUrl(args, args.run_id)}>{args.run_id}</GitHubLink>} />
          )}
          <Field name="Workflow" value={args.workflow_id} />
          <Field name="Git ref" value={args.ref} />
          {args.inputs && <JsonBlock value={args.inputs} heading="Workflow inputs" />}
        </Stack>
      ),
    }),
  ],
  [
    "get_job_logs",
    githubPresentation({
      label: "Read GitHub Actions logs",
      schema: logsArgs,
      compact: ({ args }) => <Text size="sm">{args.failed_only ? "Failed jobs only" : "Single job"}</Text>,
      request: ({ args }) => (
        <Stack gap={4}>
          <RepoTarget repo={args} path="/actions" />
          {args.run_id && (
            <Field name="Run" value={<GitHubLink href={runUrl(args, args.run_id)}>{args.run_id}</GitHubLink>} />
          )}
          <Field name="Job" value={args.job_id} />
          {args.failed_only && <Text size="sm">Failed jobs only.</Text>}
          <Field name="Return" value={args.return_content ? "Log text" : "Temporary download URL"} />
          {args.return_content && <Field name="Tail lines" value={args.tail_lines} />}
        </Stack>
      ),
      result: defineResultPreview(z.union([jobLog, failedLogs]), ({ result, context }) => {
        const args = logsArgs.safeParse(context?.arguments);
        const repo = args.success ? args.data : undefined;
        if ("failed_jobs" in result) {
          return (
            <ResultRows count={result.failed_jobs} name="failed jobs">
              {result.logs?.map((log) => (
                <JobLog key={log.job_id} log={log} repo={repo} run={repo?.run_id} />
              ))}
            </ResultRows>
          );
        }
        return <JobLog log={result} repo={repo} run={repo?.run_id} />;
      }),
    }),
  ],
]);
