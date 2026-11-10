import { BarChart } from "@mantine/charts";
import { Box, Paper, Progress, Text, Title } from "@mantine/core";
import { html } from "../report.js";

// Reviewed cache-inclusive target history: evidence.json, 3–10 November 2026 UTC.
const outcomes = [
  { day: "3 Nov", "Test failure": 3, "Build failure": 1, passed: 69 },
  { day: "4 Nov", "Test failure": 0, "Build failure": 0, passed: 179 },
  { day: "5 Nov", "Test failure": 0, "Build failure": 0, passed: 76 },
  { day: "6 Nov", "Test failure": 1, "Build failure": 0, passed: 130 },
  { day: "7 Nov", "Test failure": 3, "Build failure": 0, passed: 126 },
  { day: "8 Nov", "Test failure": 0, "Build failure": 0, passed: 98 },
  { day: "9 Nov", "Test failure": 3, "Build failure": 13, passed: 67 },
  { day: "10 Nov", "Test failure": 3, "Build failure": 1, passed: 144 },
];

export function DailyVmChart() {
  return html`<${Paper}
    withBorder
    p="md"
    my="lg"
    data-chart="recent-vm-indexed-failures"
    aria-labelledby="daily-vm-chart-title"
  >
    <${Title} order=${3} id="daily-vm-chart-title">
      Indexed VM target failures by creation day
    <//>
    <${Text} size="sm" c="dimmed" mt="xs">
      3 November 00:00–10 November 23:04 UTC. Unit: indexed target verdicts,
      including cache reuse. The chart separates failed tests from failures to
      build; it is not fresh-run or physical-execution frequency.
    <//>
    <${Box} visibleFrom="sm">
      <${BarChart}
        h=${300}
        mt="md"
        data=${outcomes}
        dataKey="day"
        type="stacked"
        series=${[
          { name: "Test failure", color: "red.6" },
          { name: "Build failure", color: "orange.6" },
        ]}
        yAxisLabel="Indexed verdicts"
        withLegend
        barProps=${{ isAnimationActive: false }}
      />
    <//>
    <${Box} hiddenFrom="sm" mt="md">
      ${outcomes.map(
        (row) =>
          html`<${Box} key=${row.day} mb="sm">
            <${Text} size="sm">
              ${`${row.day}: ${row["Test failure"]} failed tests, ${row["Build failure"]} failures to build`}
            <//>
            <${Progress}
              value=${(100 * row["Test failure"]) / 16}
              color="red.6"
              size="sm"
              aria-label=${`${row.day} failed tests`}
            />
            <${Progress}
              value=${(100 * row["Build failure"]) / 16}
              color="orange.6"
              size="sm"
              mt="xs"
              aria-label=${`${row.day} failures to build`}
            />
          <//>`,
      )}
    <//>
    <${Text} size="sm" mt="sm">Exact daily counts:</Text>
    <ul>
      ${outcomes.map(
        (row) =>
          html`<li key=${row.day}>
            ${`${row.day}: ${row["Test failure"]} failed tests, ${row["Build failure"]} failures to build, ${row.passed} passed indexed outcomes.`}
          </li>`,
      )}
    </ul>
  <//>`;
}
