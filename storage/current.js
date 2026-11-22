import {
  Alert,
  Anchor,
  Group,
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

const repo = "https://github.com/agentydragon/ducktape/";
const round = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const decimal = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });
const displayGb = (gb) => `${round.format(gb)} GB`;

function Stat({ label, value }) {
  return html`<${Paper} withBorder p="md" radius="md">
    <${Text} size="xl" fw=${700}>${value}<//>
    <${Text} size="sm">${label}<//>
  <//>`;
}

export function CurrentStorage() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    fetch("./current-evidence.json?v=20261122105825", {
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
      >Storage snapshot could not be loaded.<//
    >`;
  if (!data) return html`<${Text}>Loading storage snapshot…<//>`;

  const { pools, buckets, cache, clickhouse } = data;
  const currentPeak = Math.max(...pools.map((pool) => pool.physical_used_pct));
  const growth = pools.reduce(
    (sum, pool) => sum + pool.physical_growth_gb_7d,
    0,
  );
  const overcommitted = pools.filter((pool) => pool.committed_pct > 100);
  const chart = pools.map((pool) => ({
    pool: `Pool ${pool.pool}`,
    "7 days earlier": pool.physical_used_pct_7d_earlier,
    "22 November": pool.physical_used_pct,
  }));
  const dataSource = repo + "blob/" + data.source_devel_commit + "/";

  return html`<${Stack} gap="md" data-report="storage-current">
    <header>
      <${Anchor} href="../">← All reports<//>
      <${Title} order=${1} mt="sm">Cluster storage<//>
      <${Text} size="lg" mt="sm">
        Two HDD pools are overcommitted, and all three used more physical space
        than a week earlier. Add or free durable pool capacity before committing
        to smaller SeaweedFS volumes.
      <//>
      <${Text} size="sm" c="dimmed" mt="xs">
        22 November 2026, 10:57–10:58 UTC. Three OVH HDD pools. Capacity and
        usage comparisons use decimal GB.
      <//>
    </header>
    <${SimpleGrid} cols=${{ base: 1, sm: 3 }} spacing="md">
      <${Stat}
        value=${`${currentPeak}%`}
        label="Fullest HDD pool, physical use"
      />
      <${Stat}
        value=${`+${displayGb(growth)}`}
        label="Physical allocation change in seven days"
      />
      <${Stat}
        value=${`${overcommitted.length} of ${pools.length}`}
        label="Pools with commitments above capacity"
      />
    <//>
    <${Group} gap="md" component="nav">
      <${Anchor} href="#hdd-pools">HDD pools<//>
      <${Anchor} href="#current-actions">Interventions<//>
      <${Anchor} href="#bucket-growth">Bucket usage<//>
      <${Anchor} href="#historical-analysis">Earlier analysis<//>
      <${Anchor} href="./current-evidence.json">Current evidence<//>
    <//>

    <section id="hdd-pools">
      <${Title} order=${2}>HDD physical use and commitments<//>
      <${Paper}
        component="figure"
        withBorder
        p="md"
        my="md"
        mx=${0}
        data-chart="hdd-weekly-use"
      >
        <${BarChart}
          h=${240}
          data=${chart}
          dataKey="pool"
          series=${[
            { name: "7 days earlier", color: "gray.6" },
            { name: "22 November", color: "blue.6" },
          ]}
          yAxisProps=${{ domain: [0, 100], tickFormatter: (n) => `${n}%` }}
          valueFormatter=${(n) => `${n}%`}
          tickLine="none"
          gridAxis="y"
          withLegend
        />
        <${Text} component="figcaption" size="sm" c="dimmed" mt="sm">
          LVM data-use percentage at the snapshot and seven days earlier.
          SeaweedFS filesystem usage and diskful LINSTOR volume limits appear
          below.
        <//>
      <//>
      <${Table.ScrollContainer} minWidth=${645}>
        <${Table} striped withTableBorder aria-label="OVH HDD pool use">
          <${Table.Thead}
            ><${Table.Tr}>
              <${Table.Th} scope="col">Pool<//>
              <${Table.Th} scope="col">Capacity<//>
              <${Table.Th} scope="col">Physical use<//>
              <${Table.Th} scope="col">7-day growth<//>
              <${Table.Th} scope="col">Declared<//>
              <${Table.Th} scope="col">SeaweedFS filesystem<//>
            <//><//
          >
          <${Table.Tbody}>
            ${pools.map(
              (pool) =>
                html`<${Table.Tr} key=${pool.pool}>
                  <${Table.Th} scope="row">${pool.pool}<//>
                  <${Table.Td}>${displayGb(pool.capacity_gb)}<//>
                  <${Table.Td}>${pool.physical_used_pct}%<//>
                  <${Table.Td}>+${displayGb(pool.physical_growth_gb_7d)}<//>
                  <${Table.Td}>${pool.committed_pct}%<//>
                  <${Table.Td}>${pool.seaweed_fs_used_pct}%<//>
                <//>`,
            )}
          <//>
        <//>
      <//>
      <${Text} size="sm" mt="sm">
        Pool C has ${displayGb(pools[2].physical_free_gb)} physically free. Thin
        provisioning allows more declared volume bytes than the pool can supply
        simultaneously. Object deletion and discard free physical blocks. A
        replacement claim or larger pool changes the commitment ratio.
        ${" "}<${Anchor}
          href=${dataSource + "cluster/cdk8s/monitoring/rules.py"}
          >Metric definitions<//
        >
      <//>
    </section>

    <section id="current-actions">
      <${Title} order=${2}>Interventions<//>
      <${Stack} gap="sm" mt="md">
        <${Text}>
          <strong>1. Get the HDD pools under the commitment budget.</strong>
          With all other claims held fixed, lowering both ClickHouse limits from
          ${clickhouse.claim_gib} to ${clickhouse.candidate_claim_gib} GiB cuts
          ${displayGb(clickhouse.declared_reduction_gb_per_owner)} of declared
          demand on each of its two hosts. Both filesystems hold about 139 GB.
          Budget a temporary third replica, verify catch-up and retire the two
          old retained volumes serially.
        <//>
        <${Table.ScrollContainer} minWidth=${650}>
          <${Table}
            striped
            withTableBorder
            aria-label="Final-state 10 percent reserve scenarios"
          >
            <${Table.Thead}
              ><${Table.Tr}>
                <${Table.Th} scope="col">Pool<//>
                <${Table.Th} scope="col">Margin after ClickHouse resize<//>
                <${Table.Th} scope="col"
                  >Margin with earlier SeaweedFS targets<//
                >
                <${Table.Th} scope="col">Candidate SeaweedFS claim<//>
                <${Table.Th} scope="col">Used at that nominal size<//>
              <//><//
            >
            <${Table.Tbody}>
              ${pools.map(
                (pool) =>
                  html`<${Table.Tr} key=${pool.pool}>
                    <${Table.Th} scope="row">${pool.pool}<//>
                    <${Table.Td}>${displayGb(pool.ch_resize_only_margin_gb)}<//>
                    <${Table.Td}
                      >${displayGb(pool.resize_prior_targets.reserve_margin_gb)}<//
                    >
                    <${Table.Td}
                      >${round.format(pool.resize_to_max_targets.seaweed_target_gib)}
                      GiB<//
                    >
                    <${Table.Td}
                      >${pool.resize_to_max_targets.seaweed_used_pct_of_nominal_size}%<//
                    >
                  <//>`,
              )}
            <//>
          <//>
        <//>
        <${Text} size="sm" c="dimmed">
          Margin is 90% of pool capacity less final diskful volume limits. All
          three scenarios retain GitLab and both ClickHouse replicas. Earlier
          SeaweedFS targets were 1,180 and 1,290 GiB on B and C. The candidates
          are the largest claims within budget on B and C, with A at its current
          size. Filesystem overhead would raise the last column.
        <//>
        <${Text}>
          A resize alone leaves B and C short. The maximum claims would start B
          and C at 77% and 81% of their nominal size. Evaluate capacity or
          redistribution before choosing targets. With current commitments,
          adding about ${displayGb(pools[1].capacity_add_gb_at_10pct_reserve)}
          to B and ${displayGb(pools[2].capacity_add_gb_at_10pct_reserve)} to C
          would give each a 10% declared reserve. The larger pool also needs
          ${displayGb(pools[0].capacity_add_gb_at_10pct_reserve)} under that
          scenario.
        <//>
        <${Text}>
          <strong
            >2. Check the cache discard gap before reducing replicas.</strong
          >
          Zot uses ${decimal.format(cache.zot_fs_used_gb)} GB in the filesystem
          and LINSTOR reports about
          ${displayGb(cache.zot_allocated_gb_by_copy[0])} per diskful copy. In
          24 hours, clients made
          ${round.format(cache.zot_successful_manifest_gets_24h)} successful
          manifest GETs and received
          ${decimal.format(cache.zot_successful_blob_response_bytes_24h / 1e9)}
          GB of blob responses, including repeated and cold-cache responses.
          Compare thin-LV allocations to filesystem data and verify discard on
          both replicas before attempting a monitored reclaim. GC remains
          enabled on a 24-hour interval. ${" "}<${Anchor}
            href=${dataSource + "cluster/k8s/oci-cache/config.json"}
            >Zot policy<//
          >
        <//>
        <${Text}>
          <strong>3. Measure a Langfuse archive and restore.</strong> Langfuse
          remains the largest tracked bucket. Sample the raw-event prefix by age
          and compare archive size, restore completeness, and physical reclaim.
          Keep the November 10 age scan as a baseline. SeaweedFS reports about
          ${displayGb(pools.reduce((sum, pool) => sum + pool.seaweed_deleted_gb, 0))}
          of deleted bytes across the HDD volume servers. Observe what
          compaction and discard actually return before forecasting savings.
          ${" "}<${Anchor} href=${repo + "issues/15022"}>Archive decision<//>
        <//>
      <//>
    </section>

    <section id="bucket-growth">
      <${Title} order=${2}>Selected bucket usage<//>
      <${Table}
        striped
        withTableBorder
        mt="md"
        aria-label="Selected bucket sizes"
      >
        <${Table.Thead}
          ><${Table.Tr}>
            <${Table.Th} scope="col">Bucket<//>
            <${Table.Th} scope="col">22 November<//>
            <${Table.Th} scope="col">Change since 9 November<//>
          <//><//
        >
        <${Table.Tbody}>
          ${buckets.rows.map((bucket) => {
            const delta = (bucket.bytes - bucket.prior_bytes) / 1e9;
            return html`<${Table.Tr} key=${bucket.name}>
              <${Table.Th} scope="row">${bucket.name}<//>
              <${Table.Td}>${displayGb(bucket.bytes / 1e9)}<//>
              <${Table.Td}
                >${delta > 0 ? "+" : ""}${decimal.format(delta)} GB<//
              >
            <//>`;
          })}
        <//>
      <//>
      <${Text} size="sm" mt="sm">
        ${buckets.status_with_usage} of ${buckets.status_total} Bucket resources
        reported usage. These are logical object bytes. Devel configures
        ${data.source_configuration.mimir_retention_days} days for Mimir and
        ${data.source_configuration.loki_retention_days} days for Loki. The
        earlier series attribution and log samples retain their November 10
        timestamps. ${" "}<${Anchor} href="./current-evidence.json"
          >Measurements and queries<//
        >
      <//>
    </section>
  <//>`;
}
