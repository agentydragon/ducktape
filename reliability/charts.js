import { BarChart } from "@mantine/charts";
import { Anchor, Code, Paper, Text, Title } from "@mantine/core";
import { html } from "../report.js";

// Reviewed snapshot: evidence.json, tracker.stats, 30 Oct 22:10–1 Nov 21:45 UTC.
// These are tracked target outcomes, not physical executions or verified flakes.
const targetOutcomes = [
  { target: "VM suite", failed: 5, total: 45 },
  { target: "Bridge", failed: 2, total: 25 },
  { target: "Thread window", failed: 2, total: 25 },
  { target: "Thread browser", failed: 1, total: 26 },
];
const targetRates = targetOutcomes.map((row) => ({
  ...row,
  rate: (100 * row.failed) / row.total,
}));

// Reviewed snapshot: evidence.json, actions_job_attempt_subset.groups,
// 1 Nov 02:30–21:45 UTC. Other-event attempts (n=7) are excluded.
const jobOutcomes = [
  { event: "Devel pushes", Success: 80, Failure: 2, Cancelled: 20 },
  { event: "Pull requests", Success: 159, Failure: 64, Cancelled: 94 },
];

// Historical snapshot: 47abf0186a:reliability/evidence.json,
// pr_test_invocations.remote_error_first_error_classes,
// 28 Oct 19:55–29 Oct 20:50 UTC. The six disjoint classes sum to 47;
// pull_request and pull_request_target REMOTE_ERROR counts are 35 + 12 = 47.
// All completed PR/fork BuildBuddy test-invocation verdicts sum to 225.
const historicalRemoteErrors = [
  {
    classification: "Superseded",
    count: 16,
    detail: "Superseded-run cancellation",
    transient: null,
    sourceFirst: null,
    other: 16,
  },
  {
    classification: "Start timeout",
    count: 10,
    detail: "Firecracker start timeout",
    transient: 10,
    sourceFirst: null,
    other: null,
  },
  {
    classification: "Source first",
    count: 9,
    detail: "Source error first",
    transient: null,
    sourceFirst: 9,
    other: null,
  },
  {
    classification: "Ambiguous",
    count: 6,
    detail: "Ambiguous first progress line",
    transient: null,
    sourceFirst: null,
    other: 6,
  },
  {
    classification: "Pause refused",
    count: 5,
    detail: "Firecracker pause refused",
    transient: 5,
    sourceFirst: null,
    other: null,
  },
  {
    classification: "CAS / 5xx",
    count: 1,
    detail: "CAS precondition or HTTP 5xx",
    transient: 1,
    sourceFirst: null,
    other: null,
  },
];

const historicalEvidenceUrl =
  "https://github.com/agentydragon/ducktape/blob/47abf0186a3f5c78e60cc9759345e77c95a936ba/reliability/evidence.json";

export function TargetFailureChart() {
  return html`
    <${Paper}
      withBorder
      p="md"
      my="lg"
      data-chart="reliability-failed-target-rates"
      aria-labelledby="target-failure-chart-title"
    >
      <${Title} order=${3} id="target-failure-chart-title">
        Failed tracked target outcomes
      <//>
      <${Text} size="sm" c="dimmed" mt="xs">
        30 October 22:10–1 November 21:45 UTC. Unit: percent of tracked target
        outcomes. The tracker selectively excludes PR runs; these are not
        physical test executions or verified flake rates.
      <//>
      <${BarChart}
        h=${300}
        mt="md"
        data=${targetRates}
        dataKey="target"
        orientation="vertical"
        series=${[{ name: "rate", label: "Failed outcomes", color: "red.6" }]}
        xAxisProps=${{ domain: [0, 16], tickFormatter: (value) => `${value}%` }}
        yAxisProps=${{ width: 100 }}
        valueFormatter=${(value) => `${value.toFixed(1)}%`}
        withBarValueLabel
        valueLabelProps=${{ position: "right" }}
        barProps=${{ isAnimationActive: false }}
      />
      <${Text} size="sm" mt="sm">Exact counts:</Text>
      <ul>
        ${targetOutcomes.map(
          ({ target, failed, total }) => html`
            <li key=${target}>
              ${`${target}: ${failed}/${total} tracked outcomes failed (${((100 * failed) / total).toFixed(1)}%).`}
            </li>
          `,
        )}
      </ul>
      <${Text} size="sm">
        <${Anchor} href="evidence.json">Reviewed target evidence<//>.
      <//>
    <//>
  `;
}

export function JobOutcomeChart() {
  return html`
    <${Paper}
      withBorder
      p="md"
      my="lg"
      data-chart="reliability-job-outcomes"
      aria-labelledby="job-outcome-chart-title"
    >
      <${Title} order=${3} id="job-outcome-chart-title">
        Test & Build job attempts by event
      <//>
      <${Text} size="sm" c="dimmed" mt="xs">
        ${"1 November 02:30–21:45 UTC. Unit: GitHub Actions "}
        <${Code}>bazel-ci / Test & Build<//>
        ${" job attempts created in this subset. Cancelled attempts are shown but excluded from conclusive failure denominators. Seven other-event attempts are outside this comparison."}
      <//>
      <${BarChart}
        h=${260}
        mt="md"
        data=${jobOutcomes}
        dataKey="event"
        type="stacked"
        orientation="vertical"
        series=${[
          { name: "Success", color: "teal.6" },
          { name: "Failure", color: "red.6" },
          { name: "Cancelled", color: "gray.6" },
        ]}
        xAxisProps=${{ domain: [0, 320] }}
        yAxisProps=${{ width: 100 }}
        xAxisLabel="Job attempts"
        withLegend
        barProps=${{ isAnimationActive: false }}
      />
      <${Text} size="sm" mt="sm">Exact counts:</Text>
      <ul>
        ${jobOutcomes.map(
          ({ event, Success, Failure, Cancelled }) => html`
            <li key=${event}>
              ${`${event}: ${Success} success, ${Failure} failure, ${Cancelled} cancelled (${Success + Failure + Cancelled} attempts).`}
            </li>
          `,
        )}
      </ul>
      <${Text} size="sm">
        ${"Failures include source, test, runner and infrastructure errors. "}
        <${Anchor} href="evidence.json">Reviewed job evidence<//>.
      <//>
    <//>
  `;
}

export function HistoricalRemoteErrorChart() {
  return html`
    <${Paper}
      withBorder
      p="md"
      my="lg"
      data-chart="reliability-historical-remote-errors"
      aria-labelledby="historical-remote-chart-title"
    >
      <${Title} order=${3} id="historical-remote-chart-title">
        Earlier PR remote-error classifications
      <//>
      <${Text} size="sm" c="dimmed" mt="xs">
        ${"Historical measurement, 28 October 19:55–29 October 20:50 UTC. Unit: completed PR/fork BuildBuddy test invocations. The 47 "}
        <${Code}>REMOTE_ERROR<//>
        ${" verdicts are a subset of 225 completed invocations. Red marks the 16 transient infrastructure cases; blue marks source error first. This chart does not describe the current report window."}
      <//>
      <${BarChart}
        h=${350}
        mt="md"
        data=${historicalRemoteErrors}
        dataKey="classification"
        type="stacked"
        orientation="vertical"
        series=${[
          {
            name: "transient",
            label: "Transient infrastructure",
            color: "red.6",
          },
          { name: "sourceFirst", label: "Source error first", color: "blue.6" },
          {
            name: "other",
            label: "Cancellation or ambiguous",
            color: "gray.6",
          },
        ]}
        xAxisProps=${{ domain: [0, 20] }}
        yAxisProps=${{ width: 105 }}
        xAxisLabel="PR/fork test invocations"
        withLegend
        withBarValueLabel
        valueLabelProps=${{ position: "right" }}
        barProps=${{ isAnimationActive: false }}
      />
      <${Text} size="sm" mt="sm">Exact classification counts:</Text>
      <ul>
        ${historicalRemoteErrors.map(
          ({ classification, count, detail }) => html`
            <li key=${classification}>${`${detail}: ${count} invocations.`}</li>
          `,
        )}
      </ul>
      <${Text} size="sm">
        ${"The transient infrastructure cases are 10 + 5 + 1 = 16/225 (7.1%). "}
        <${Anchor} href=${historicalEvidenceUrl}>Archived reviewed evidence<//>.
      <//>
    <//>
  `;
}
