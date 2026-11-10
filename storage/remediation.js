import {
  Accordion,
  Alert,
  Anchor,
  Badge,
  Button,
  Checkbox,
  Code,
  Group,
  NativeSelect,
  Paper,
  SimpleGrid,
  Stack,
  Table,
  Text,
  Title,
} from "@mantine/core";
import { BarChart } from "@mantine/charts";
import { useEffect, useState } from "react";
import { html } from "../report.js";

const DECISION_KEY = "ducktape-storage-remediation-decisions-v1";
const decisionChoices = [
  { value: "considering", label: "Considering" },
  { value: "pursue", label: "Pursue" },
  { value: "defer", label: "Defer" },
  { value: "decline", label: "Decline" },
];

function loadDecisions() {
  try {
    const value = JSON.parse(localStorage.getItem(DECISION_KEY) || "{}");
    return value && typeof value === "object" && !Array.isArray(value)
      ? value
      : {};
  } catch {
    return {};
  }
}

function Figure({ title, caption, children, slug }) {
  return html`<${Paper}
    component="figure"
    withBorder
    p="md"
    m=${0}
    data-chart=${slug}
  >
    <${Title} order=${3} mb="md">${title}<//>
    ${children}
    <${Text} component="figcaption" c="dimmed" size="sm" mt="sm">${caption}<//>
  <//>`;
}

function Options({ data }) {
  const [decisions, setDecisions] = useState(loadDecisions);
  const [filter, setFilter] = useState("all");
  const visible = data.options.filter((option) =>
    filter === "all"
      ? true
      : (decisions[option.id] || "considering") === filter,
  );

  function updateDecision(id, value) {
    setDecisions((old) => {
      const next = { ...old, [id]: value };
      try {
        localStorage.setItem(DECISION_KEY, JSON.stringify(next));
      } catch {
        // The controls still work for this page session if storage is unavailable.
      }
      return next;
    });
  }

  function exportDecisions() {
    const blob = new Blob(
      [
        JSON.stringify(
          { snapshot_utc: data.snapshot_utc, decisions },
          null,
          2,
        ) + "\n",
      ],
      { type: "application/json" },
    );
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "storage-decisions.json";
    anchor.click();
    URL.revokeObjectURL(url);
  }

  return html`<${Stack} component="section" id="options" gap="md" my="xl">
    <${Title} order=${2}>Intervention options<//>
    <${Text}>
      Ranked by potential benefit, effort and recovery impact. Amounts use
      different measures; options overlap and cannot be summed blindly. Choices
      are saved only in this browser and do not change the cluster.
    <//>
    <${Group} align="end">
      <${NativeSelect}
        label="Show decisions"
        value=${filter}
        onChange=${(event) => setFilter(event.currentTarget.value)}
        data=${[{ value: "all", label: "All options" }, ...decisionChoices]}
      />
      <${Button} variant="default" onClick=${exportDecisions}
        >Export decisions<//
      >
      <${Text} size="sm" c="dimmed"
        >${visible.length} of ${data.options.length} options<//
      >
    <//>
    ${
      visible.length === 0
        ? html`<${Text} c="dimmed">No options match this decision.<//>`
        : html`<${SimpleGrid} cols=${{ base: 1, md: 2 }} spacing="md">
            ${visible.map(
              (option) =>
                html`<${Paper}
                  component="article"
                  withBorder
                  p="md"
                  radius="md"
                  key=${option.id}
                >
                  <${Stack} gap="sm">
                    <${Group} justify="space-between" align="start">
                      <${Text} fw=${700}>${option.rank}. ${option.title}<//>
                      <${Badge} variant="light">${option.category}<//>
                    <//>
                    <${Text} size="lg" fw=${700}>${option.gain}<//>
                    <${Text} size="sm" c="dimmed"
                      >${option.gain_type} · ${option.effort} effort<//
                    >
                    <${Text} size="sm">${option.detail}<//>
                    <${Text} size="sm"
                      ><strong>Recovery:</strong> ${option.risk}<//
                    >
                    <${Text} size="sm" c="dimmed"
                      ><strong>Evidence:</strong> ${option.confidence}<//
                    >
                    ${option.link ? html`<${Anchor} href=${option.link} size="sm">Supporting issue or plan<//>` : null}
                    <${NativeSelect}
                      label=${`Decision for ${option.title}`}
                      value=${decisions[option.id] || "considering"}
                      onChange=${(event) => updateDecision(option.id, event.currentTarget.value)}
                      data=${decisionChoices}
                    />
                  <//>
                <//>`,
            )}
          <//>`
    }
  <//>`;
}

function BudgetScenario({ data }) {
  const [selected, setSelected] = useState({});
  const choices = data.options.filter((option) => option.node_delta_gb);
  const scenario = data.pools.map((pool) => {
    const delta = choices.reduce(
      (sum, option) =>
        sum + (selected[option.id] ? option.node_delta_gb[pool.node] || 0 : 0),
      0,
    );
    return {
      node: pool.node,
      capacity: pool.capacity_gb,
      current: pool.declared_gb,
      scenario: pool.declared_gb + delta,
      budget: pool.capacity_gb * (1 - data.reserve_fraction),
      free: pool.free_gb,
    };
  });
  function toggle(option, checked) {
    setSelected((old) => ({
      ...old,
      ...(checked && option.exclusive_with
        ? { [option.exclusive_with]: false }
        : {}),
      [option.id]: checked,
    }));
  }
  const rows = scenario.map((pool) => ({
    ...pool,
    headroom: pool.budget - pool.scenario,
  }));
  return html`<${Stack} component="section" id="budget" gap="md" my="xl">
    <${Title} order=${2}>Test a pool-budget scenario<//>
    <${Text}>
      The budget assumes a 10% reserve; it is a planning choice, not a
      configured policy. Select changes to compare declared limits with each
      node’s capacity. Data deletion and compression do not change these limits.
    <//>
    <${SimpleGrid} cols=${{ base: 1, md: 2 }} spacing="lg">
      <${Paper} withBorder p="md" radius="md">
        <${Stack} gap="sm">
          <${Text} fw=${700}>Simulated changes<//>
          ${choices.map(
            (option) =>
              html`<${Checkbox}
                key=${option.id}
                label=${option.title}
                checked=${Boolean(selected[option.id])}
                onChange=${(event) => toggle(option, event.currentTarget.checked)}
              />`,
          )}
          <${Button}
            variant="subtle"
            size="xs"
            onClick=${() => setSelected({})}
          >
            Clear scenario
          <//>
        <//>
      <//>
      <${Figure}
        title="Pool capacity and declared limits"
        caption="GB, summed diskful volume limits on each node. The selected limits are arithmetic projections; migration feasibility and current growth are not modeled."
        slug="storage-pool-budget"
      >
        <${BarChart}
          h=${290}
          data=${scenario}
          dataKey="node"
          series=${[
            { name: "capacity", label: "Capacity", color: "gray.6" },
            { name: "current", label: "Current limits", color: "orange.6" },
            { name: "scenario", label: "Scenario limits", color: "blue.6" },
          ]}
          withLegend
          barProps=${{ isAnimationActive: false }}
          yAxisProps=${{ width: 54, tickFormatter: (value) => `${value}` }}
          barChartProps=${{ margin: { right: 12, bottom: 8 } }}
          valueFormatter=${(value) => `${Number(value).toFixed(0)} GB`}
        />
      <//>
    <//>
    <${Table.ScrollContainer} minWidth=${590}>
      <${Table} striped withTableBorder>
        <${Table.Thead}
          ><${Table.Tr}>
            <${Table.Th}>Node<//><${Table.Th}>Physical<//
            ><${Table.Th}>Free now<//> <${Table.Th}>Limits now<//
            ><${Table.Th}>Scenario<//><${Table.Th}>Under 90% budget<//>
          <//><//
        >
        <${Table.Tbody}>
          ${rows.map(
            (row) =>
              html`<${Table.Tr} key=${row.node}>
                <${Table.Td}>${row.node}<//>
                <${Table.Td}>${row.capacity.toFixed(0)} GB<//>
                <${Table.Td}>${row.free.toFixed(0)} GB<//>
                <${Table.Td}>${row.current.toFixed(0)} GB<//>
                <${Table.Td}>${row.scenario.toFixed(0)} GB<//>
                <${Table.Td}
                  >${row.headroom >= 0 ? "+" : ""}${row.headroom.toFixed(0)}
                  GB<//
                >
              <//>`,
          )}
        <//>
      <//>
    <//>
    <${Alert} title="GitLab is an active migration dependency" color="yellow">
      GitLab claim removal is conditional on the Forgejo-to-GitLab plan ending
      or changing. Selecting it here does not assert that retirement is safe.
    <//>
  <//>`;
}

function LangfuseEvidence({ data }) {
  const ageRows = data.langfuse_ages.map((row) => ({
    age: row.age,
    OTLP: row.otel_gb,
    Legacy: row.legacy_gb,
    total: row.otel_gb + row.legacy_gb,
  }));
  const compression = data.compression_sample;
  const compressionRows = [
    { kind: "Raw", MB: compression.uncompressed_mb },
    { kind: "gzip 6", MB: compression.gzip_level_6_mb },
    { kind: "zstd 3", MB: compression.zstd_level_3_mb },
  ];
  const columns = data.clickhouse_columns.map((row) => ({
    name: row.column,
    GB: row.compressed_gb_per_replica,
  }));
  const evidence = data.clickhouse_evidence;
  return html`<${Stack} component="section" id="langfuse" gap="md" my="xl">
    <${Title} order=${2}>Why Langfuse is large<//>
    <${Text}>
      Raw ingestion files and ClickHouse serve different recovery needs. A
      sampled August request had no ClickHouse row for its trace; historical raw
      exception events and deliberately removed request spans are not proven
      reconstructible.
      <${Anchor} href="https://github.com/agentydragon/ducktape/issues/15022">
        Investigation and coverage details<//
      >.
    <//>
    <${SimpleGrid} cols=${{ base: 1, lg: 2 }} spacing="md">
      <${Figure}
        title="Raw S3 objects by age"
        caption="GB logical, by file modification time; two Seaweed copies. Expiring objects over 30 days is an upper bound of 842 GB physical before archive costs, vacuum and discard."
        slug="langfuse-s3-age"
      >
        <${BarChart}
          h=${245}
          data=${ageRows}
          dataKey="age"
          type="stacked"
          series=${[
            { name: "OTLP", color: "blue.6" },
            { name: "Legacy", color: "grape.6" },
          ]}
          withLegend
          barProps=${{ isAnimationActive: false }}
          yAxisProps=${{ width: 48, tickFormatter: (value) => Number(value).toFixed(0) }}
          barChartProps=${{ margin: { right: 10, bottom: 8 } }}
          valueFormatter=${(value) => `${Number(value).toFixed(1)} GB`}
        />
        <${Table} striped mt="sm">
          <${Table.Thead}
            ><${Table.Tr}
              ><${Table.Th}>Age<//><${Table.Th}>OTLP<//><${Table.Th}>Legacy<//
              ><${Table.Th}>Total<//><//
            ><//
          >
          <${Table.Tbody}
            >${ageRows.map(
              (row) =>
                html`<${Table.Tr} key=${row.age}>
                  <${Table.Td}>${row.age}<//
                  ><${Table.Td}>${row.OTLP.toFixed(1)}<//
                  ><${Table.Td}>${row.Legacy.toFixed(1)}<//
                  ><${Table.Td}>${row.total.toFixed(1)} GB<//>
                <//>`,
            )}<//
          >
        <//>
      <//>
      <${Figure}
        title="Lossless compression sample"
        caption="33 objects, 20.7 MB total; sampled by metadata pages, prefix and month, not by byte weight. This is not a fleet-wide savings estimate."
        slug="langfuse-compression-sample"
      >
        <${BarChart}
          h=${245}
          data=${compressionRows}
          dataKey="kind"
          series=${[{ name: "MB", color: "teal.6" }]}
          withBarValueLabel
          barProps=${{ isAnimationActive: false }}
          yAxisProps=${{ width: 48, tickFormatter: (value) => Number(value).toFixed(0) }}
          barChartProps=${{ margin: { right: 10, bottom: 8 } }}
          valueFormatter=${(value) => `${Number(value).toFixed(1)} MB`}
        />
        <${Text} size="sm" mt="sm">
          ${`zstd level 3: ${Math.round((compression.zstd_level_3_mb / compression.uncompressed_mb) * 100)}% of sampled bytes; gzip level 6: ${Math.round((compression.gzip_level_6_mb / compression.uncompressed_mb) * 100)}%. An archive still needs checksums and a tested restore path.`}
        <//>
      <//>
    <//>
    <${Figure}
      title="ClickHouse column footprint"
      caption="Compressed GB on one ClickHouse replica, from active parts; there are two replicas. These are footprints, not proven savings. Full metadata and both parameter columns overlap in sampled tool definitions."
      slug="clickhouse-columns"
    >
      <${BarChart}
        h=${260}
        data=${columns}
        dataKey="name"
        orientation="vertical"
        series=${[{ name: "GB", color: "indigo.6" }]}
        yAxisProps=${{ width: 132 }}
        withBarValueLabel
        barProps=${{ isAnimationActive: false }}
        barChartProps=${{ margin: { right: 48, bottom: 8 } }}
        valueFormatter=${(value) => `${Number(value).toFixed(1)} GB`}
      />
      <${Table} striped mt="sm">
        <${Table.Thead}
          ><${Table.Tr}
            ><${Table.Th}>Column<//><${Table.Th}>GB per replica<//><//
          ><//
        >
        <${Table.Tbody}
          >${columns.map((row) => html`<${Table.Tr} key=${row.name}><${Table.Td}>${row.name}<//><${Table.Td}>${row.GB.toFixed(2)}<//><//>`)}<//
        >
      <//>
    <//>
    <${Accordion} variant="separated">
      <${Accordion.Item} value="mechanism">
        <${Accordion.Control}>What the ClickHouse investigation found<//>
        <${Accordion.Panel}>
          <${Stack} gap="sm">
            <${Text}
              >${evidence.sample_window}. ${evidence.sample_top_parameter}<//
            >
            <${Text}
              >${evidence.sample_top_metadata} ${evidence.sample_tool_equal}<//
            >
            <${Text}
              >${evidence.sample_core_equal} ${evidence.view_definition}<//
            >
            <${Text}>${evidence.recent}<//>
            <${Text}>${evidence.codec}<//>
            <${Text} c="dimmed">${evidence.limits}<//>
            <${Anchor}
              href="https://github.com/langfuse/langfuse/blob/v4.67.0/packages/shared/src/server/otel/OtelIngestionProcessor.ts"
            >
              Pinned Langfuse OTLP processor
            <//>
            <${Anchor}
              href="https://github.com/agentydragon/ducktape/blob/devel/cluster/debug/langfuse_tool_parameters/README.md"
            >
              Investigation and reproducible queries
            <//>
          <//>
        <//>
      <//>
    <//>
  <//>`;
}

export function StorageRemediation() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    fetch(new URL("./remediation-evidence.json", import.meta.url), {
      signal: controller.signal,
    })
      .then((response) => {
        if (!response.ok) throw new Error(`Evidence HTTP ${response.status}`);
        return response.json();
      })
      .then(setData)
      .catch((reason) => {
        if (reason.name !== "AbortError") setError(true);
      });
    return () => controller.abort();
  }, []);
  if (error)
    return html`<${Alert} color="red"
      >Storage remediation evidence could not be loaded.<//
    >`;
  if (!data) return html`<${Text}>Loading storage remediation evidence…<//>`;
  const overcommitted = data.pools.filter(
    (pool) => pool.declared_gb > pool.capacity_gb,
  );
  return html`<${Stack}
    component="section"
    gap="md"
    data-report="storage-remediation"
  >
    <header>
      <${Badge} variant="light">Read-only snapshot · 10 November 2026 UTC<//>
      <${Title} order=${1} mt="sm">Make HDD commitments fit<//>
      <${Text} mt="sm" size="lg">
        Reduce the pool limits that exceed physical capacity while preserving
        the data and recovery guarantees that matter.
      <//>
      <${Text} c="dimmed" size="sm" mt="sm">
        Pool and filer observations were collected separately around 19:13–19:20
        UTC. Bucket usage timestamps can be older. Counts and projections are
        not live.
      <//>
    </header>
    <${SimpleGrid} cols=${{ base: 1, sm: 3 }} spacing="md">
      <${Paper} withBorder p="md" radius="md"
        ><${Text} size="xl" fw=${700}>${overcommitted.length} pools<//
        ><${Text} size="sm">Declared limits exceed physical capacity<//><//
      >
      <${Paper} withBorder p="md" radius="md"
        ><${Text} size="xl" fw=${700}>613 / 276 GB<//
        ><${Text} size="sm">Excess limits on 103656 / 103711<//><//
      >
      <${Paper} withBorder p="md" radius="md"
        ><${Text} size="xl" fw=${700}>421 GB<//
        ><${Text} size="sm"
          >Langfuse raw objects older than 30 days, logical<//
        ><//
      >
    <//>
    <${Group} component="nav" gap="md">
      <${Anchor} href="#budget">Budget scenario<//>
      <${Anchor} href="#langfuse">Langfuse evidence<//>
      <${Anchor} href="#options">Options<//>
      <${Anchor} href="#prior-inventory">9 Nov inventory<//>
      <${Anchor} href="./remediation-evidence.json">Evidence JSON<//>
    <//>
    <${Alert} color="blue" title="Two measures, two jobs">
      Declared limits can exceed a thin pool even while it has free space.
      Removing old data can return physical blocks after compaction and discard,
      but it does not shrink a PVC. Seaweed's HDD volumes are single-copy at the
      Piraeus layer; Seaweed provides two object copies.
    <//>
    <${BudgetScenario} data=${data} />
    <${LangfuseEvidence} data=${data} />
    <${Options} data=${data} />
    <${Text} c="dimmed" size="sm">
      Evidence includes the original observation window, uncertainty,
      interventions and source links in
      <${Anchor} href="./remediation-evidence.json">remediation-evidence.json<//
      >. No retention, replication, or volume change is implied by this report.
    <//>
  <//>`;
}
