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

function ObservabilityEvidence({ data }) {
  const evidence = data.observability;
  const mib = evidence.mimir_series.map((row) => ({
    name: row.name,
    thousands: row.series / 1000,
  }));
  return html`<${Stack} component="section" id="observability" gap="md" my="xl">
    <${Title} order=${2}>Mimir and Loki: what is using space<//>
    <${Text}>
      ${`Read-only observations at ${evidence.observed_utc}. The charts use different measures: Mimir blocks are compressed object bytes, series counts describe current cardinality, and Loki's index estimates uncompressed log volume. They cannot be added together.`}
    <//>
    <${SimpleGrid} cols=${{ base: 1, lg: 2 }} spacing="md">
      <${Figure}
        title="Mimir blocks by data age"
        caption="GB logical, 171 block metadata records and their filer object sizes. A live S3 listing found 239.7 GB; the bucket status and exporter reported 272.0 and 211.7 GB. Blocks crossing a cutoff are counted on the newer side."
        slug="mimir-age"
      >
        <${BarChart}
          h=${245}
          data=${evidence.mimir_block_ages}
          dataKey="age"
          type="stacked"
          series=${[
            { name: "index", label: "Index", color: "grape.6" },
            {
              name: "chunks_other",
              label: "Chunks and other",
              color: "blue.6",
            },
          ]}
          withLegend
          barProps=${{ isAnimationActive: false }}
          yAxisProps=${{ width: 48, tickFormatter: (value) => Number(value).toFixed(0) }}
          barChartProps=${{ margin: { right: 10, bottom: 8 } }}
          valueFormatter=${(value) => `${Number(value).toFixed(1)} GB`}
        />
        <${Table} striped mt="sm">
          <${Table.Thead}
            ><${Table.Tr}><${Table.Th}>Age<//><${Table.Th}>Total<//><//><//
          >
          <${Table.Tbody}>
            ${evidence.mimir_block_ages.map((row) => html`<${Table.Tr} key=${row.age}><${Table.Td}>${row.age}<//><${Table.Td}>${(row.index + row.chunks_other).toFixed(1)} GB<//><//>`)}
          <//>
        <//>
      <//>
      <${Figure}
        title="Mimir current series"
        caption="Thousands of series at one instant. Source-less HTTP entries still identify destinations; filtering them loses that history. Series counts do not establish compressed-byte savings."
        slug="mimir-series"
      >
        <${BarChart}
          h=${245}
          data=${mib}
          dataKey="name"
          orientation="vertical"
          series=${[{ name: "thousands", label: "Thousands", color: "indigo.6" }]}
          yAxisProps=${{ width: 150 }}
          withBarValueLabel
          barProps=${{ isAnimationActive: false }}
          barChartProps=${{ margin: { right: 42, bottom: 8 } }}
          valueFormatter=${(value) => `${Number(value).toFixed(0)}k`}
        />
        <${Text} size="sm" mt="sm">
          133,686 of 1,073,249 current series are source-less Hubble HTTP flows.
          All Hubble flow series number 290,331. Historical per-destination
          attribution is intentional; check ad-hoc uses before filtering.
        <//>
      <//>
    <//>
    <${Figure}
      title="Loki: two large sources over time"
      caption="Indexed, uncompressed GB for selected jobs, not on-disk bytes. Thirty-day queries can count overlapping chunks at boundaries; other jobs and streams without a job label are omitted."
      slug="loki-sources"
    >
      <${BarChart}
        h=${245}
        data=${evidence.loki_indexed_jobs}
        dataKey="age"
        type="stacked"
        series=${[
          { name: "Talos", label: "Talos node logs", color: "teal.6" },
          { name: "KubeState", label: "kube-state-metrics", color: "orange.6" },
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
            ><${Table.Th}>Age<//><${Table.Th}>Talos<//
            ><${Table.Th}>kube-state<//><//
          ><//
        >
        <${Table.Tbody}>
          ${evidence.loki_indexed_jobs.map((row) => html`<${Table.Tr} key=${row.age}><${Table.Td}>${row.age}<//><${Table.Td}>${row.Talos.toFixed(1)} GB<//><${Table.Td}>${row.KubeState.toFixed(1)} GB<//><//>`)}
        <//>
      <//>
    <//>
    <${Accordion} variant="separated">
      <${Accordion.Item} value="mimir-details">
        <${Accordion.Control}>Mimir: retention and series details<//>
        <${Accordion.Panel}>
          <${Stack} gap="sm">
            <${Text}>
              The S3 listing contained 963 objects and 239.718 GB logical; filer
              metadata reported 239.927 GB including bookkeeping. The Bucket
              status and exporter disagreed, so their values are not
              interchangeable. The 171 block metadata records cover June 2 to
              November 10, about 161 days, despite a 365-day retention setting.
            <//>
            <${Text}>
              Blocks older than 90 days hold 100.238 GB logical, or about 200.5
              GB with two Seaweed copies. A 90-day limit would lose all older
              metric history; 180 days would remove none of the observed blocks.
              Compaction, deletion markers, vacuum and discard affect actual
              reclaim. Neither change reduces declared PVC limits.
            <//>
            <${Text}>
              Of 1,073,249 current series, 290,331 are Hubble flows, including
              207,086 L7 HTTP and 133,686 L7 HTTP with no source. The last group
              is 12.5% of all current series and still identifies destinations.
              Historical flow attribution is intentional; inspect ad-hoc uses
              before filtering. A filter changes future ingestion only, and its
              compressed-byte savings are unmeasured. Dropping a label can
              collapse distinct series into duplicate samples. The Hubble
              ServiceMonitor scrapes every 60 seconds. No exact reference to
              this metric was found in repository dashboards and rules or live
              PrometheusRules; ad-hoc and externally provisioned uses are
              unknown.
            <//>
            <${Text}>
              Other large families include API-server request-duration buckets
              (43,740 current series), request-body-size buckets (37,152), etcd
              request-duration buckets (30,710), and API-server SLI buckets
              (25,352). ControlPlaneLeasePutLatency alerts use the first
              histogram. Historical all-series queries at 7 and 30 days timed
              out, so these current shares do not establish historical shares.
            <//>
          <//>
        <//>
      <//>
      <${Accordion.Item} value="audit">
        <${Accordion.Control}>Why Talos audit logs are noisy<//>
        <${Accordion.Panel}>
          <${Stack} gap="sm">
            <${Text}>
              Four five-minute windows attributed 75–86% of Talos log bytes to
              auditd. All 3,000 sampled AVC denials had permissive=1; 2,943
              involved DRBD and unlabeled_t, and 2,996 reported directory
              writes. These sampled denials were not enforced; that does not
              establish cluster-wide SELinux policy. In nine 1,000-line samples,
              86.4% of audit bytes were associated with permissive
              DRBD/unlabeled events, ranging from 18.4% to 99.9% by window.
              These samples were stratified by time and node, not by bytes.
            <//>
            <${Text}>
              Audit events include multiple PATH, SYSCALL, CWD, PROCTITLE and
              EOE records. Preserve security evidence while investigating the
              labeling and policy cause; do not silence every denial or manually
              relabel volumes. Talos chunks in four short windows compressed to
              5.9–7.3% of scanned raw bytes.
            <//>
            <${Text}>
              If all auditd output disappeared, the 286.384 GB of indexed Talos
              raw volume over 30 days, multiplied by the sampled 75–86% audit
              share, 5.9–7.3% compression and two copies, suggests roughly 25–36
              GB physical per 30 days. This is a low-confidence broad scenario,
              not measured reclaim or a recommendation to discard all audit
              history; a targeted fix would save less.
            <//>
            <${Text}>
              Vector currently labels streams by job and node; the Talos service
              is a JSON field. Service-specific retention cannot select those
              existing streams. Splitting future streams by a stable service
              label could enable targeted retention, with security, cardinality
              and lost-history tradeoffs.
            <//>
          <//>
        <//>
      <//>
      <${Accordion.Item} value="ksm">
        <${Accordion.Control}>The older kube-state-metrics spike<//>
        <${Accordion.Panel}>
          <${Stack} gap="sm">
            <${Text}>
              ${"The August 12–September 10 spike was repeated missing spec.suspend errors. All 1,500 sampled lines had that pattern; "}
              <${Anchor}
                href="https://github.com/agentydragon/ducktape/pull/5864"
              >
                the September 9 fix<//
              >
              ${" is present in the live configuration. Four short chunk samples compressed to 4.1–5.9%, so the 182.569 GB indexed raw estimate is not disk reclaim."}
            <//>
            <${Text}>
              The old entries should age out under 90-day retention. A query
              just beyond 90 days still returned a 19.7 MB index estimate, so
              neither complete deletion nor physical reclaim is proven.
            <//>
          <//>
        <//>
      <//>
      <${Accordion.Item} value="methods">
        <${Accordion.Control}>Methods and reproducible aggregate queries<//>
        <${Accordion.Panel}>
          <${Stack} gap="sm">
            <${Text}>
              The read-only snapshot is non-atomic and uses decimal bytes. The
              Mimir and Loki buckets use two Seaweed object copies on
              single-copy Piraeus HDD claims. Mimir block age uses each
              meta.json maxTime joined to filer object sizes; crossing blocks
              count on the newer side. Object modification time is not data age.
            <//>
            <${Text}>
              Loki's index-volume API returns approximate uncompressed chunk
              bytes; chunks overlapping window boundaries can be counted more
              than once. The deployment allows at most 30 days plus one hour per
              query. The chart excludes streams without a job label. The Loki
              Bucket status reported 120.654 GB logical, while the exporter
              reported 95.890 GB logical and 241.336 GB physical; these do not
              establish a precise per-source physical share.
            <//>
            <${Anchor}
              href="https://github.com/grafana/loki/blob/v3.6.11/pkg/storage/stores/shipper/indexshipper/tsdb/store.go"
            >
              Loki's indexed size uses approximate uncompressed chunk bytes
            <//>
            <${Text}>
              These PromQL expressions reproduce the current series counts and
              leading API-server families, using a local forward to the Mimir
              gateway:
            <//>
            <${Code}
              block
              style=${{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}
              >${`count({__name__!=""})
count(hubble_flows_processed_total)
count(hubble_flows_processed_total{type="L7",subtype="HTTP",source=""})
topk(30, count by (__name__) ({job="apiserver"}))`}<//
            >
            <${Text}>
              This LogQL expression aggregates Talos bytes by service and level
              in bounded five-minute windows, using a local forward to Loki
              read:
            <//>
            <${Code}
              block
              style=${{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}
              >${`sum by (talos_service, talos_level) (
  bytes_over_time({job="talos-node"} | json [5m])
)`}<//
            >
            <${Text}>
              For job-level history, query /loki/api/v1/index/volume with
              selector {job=~".+"}, targetLabels=job and explicit 30-day
              intervals. For audit attribution, correlate AVC, PATH, SYSCALL and
              other records by audit event ID in bounded, time- and
              node-stratified samples. No raw log lines or credentials were
              saved in this report.
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
        Pool and Langfuse observations were collected around 19:13–19:20 UTC;
        Mimir and Loki evidence was collected around 21:12–21:28 UTC. Bucket
        usage timestamps can be older. Counts and projections are not live.
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
      <${Anchor} href="#observability">Mimir and Loki<//>
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
    <${ObservabilityEvidence} data=${data} />
    <${Options} data=${data} />
    <${Text} c="dimmed" size="sm">
      Evidence includes the original observation window, uncertainty,
      interventions and source links in
      <${Anchor} href="./remediation-evidence.json">remediation-evidence.json<//
      >. No retention, replication, or volume change is implied by this report.
    <//>
  <//>`;
}
