import { useEffect, useState } from "react";
import {
  Anchor,
  Blockquote,
  NativeSelect,
  Paper,
  SimpleGrid,
  Stack,
  Table,
  Text,
  Title,
} from "@mantine/core";
import { BarChart, LineChart } from "@mantine/charts";
import { html } from "../report.js";

const invocation = (id) => `https://app.buildbuddy.io/invocation/${id}`;
const fixed = (value, digits = 2) => Number(value).toFixed(digits);

function ResourceContent({ snapshot }) {
  const [window, setWindow] = useState("runs");
  const timeline = snapshot.timeline
    .filter(
      (row) =>
        window === "day" ||
        (row.utc >= "2026-11-09T00:35:00Z" &&
          row.utc <= "2026-11-09T03:20:00Z"),
    )
    .map((row) => ({ ...row, time: row.utc.slice(5, 16).replace("T", " ") }));
  const charts = [
    {
      id: "resource-cpu",
      title: "VM CPU use (cores; four allocated)",
      series: [{ name: "vmCores", label: "VM cores", color: "blue.6" }],
      domain: [0, 4.2],
    },
    {
      id: "resource-disk",
      title: "Host work disk busy time (%)",
      series: [
        { name: "diskBusyPct", label: "/dev/vdk busy", color: "violet.6" },
      ],
      domain: [0, 100],
    },
    {
      id: "resource-memory",
      title: "Host available RAM and swap (GiB)",
      series: [
        {
          name: "hostMemAvailableGiB",
          label: "Available RAM",
          color: "gray.6",
        },
        { name: "hostSwapGiB", label: "Host swap", color: "teal.6" },
        { name: "vmSwapGiB", label: "VM swap", color: "blue.6" },
      ],
      domain: [0, "auto"],
    },
    {
      id: "resource-pressure",
      title: "Host pressure stalls (%)",
      series: [
        { name: "ioStallPct", label: "I/O full", color: "red.6" },
        { name: "memoryStallPct", label: "Memory full", color: "orange.6" },
        { name: "cpuWaitPct", label: "CPU some", color: "indigo.6" },
      ],
      domain: [0, "auto"],
    },
  ];
  const pair = snapshot.fixture_pair;
  const pairData = [
    ["Command", "command_seconds"],
    ["Setup", "setup_seconds"],
    ["Imports", "imports_seconds"],
    ["Scenario", "scenario_seconds"],
  ].map(([phase, field]) => ({
    phase,
    Baseline: pair.baseline[field],
    Candidate: pair.candidate[field],
    "Warm control": pair.warm_baseline_control[field],
  }));
  return html`<${Stack} gap="md">
    <${Text}
      >Historical Mimir window: ${snapshot.window.start_utc} →
      ${snapshot.window.end_utc}. Each selected series has 1,441 one-minute
      samples; rates use three-minute windows. Short bursts and phase boundaries
      are smoothed.<//
    >
    <${Blockquote}
      >Two physical Firecracker command windows averaged 1.08 and 1.65 of four
      CPU cores, while the shared backing disk averaged 65% and 69% busy. This
      supports a fast-storage/host-isolation experiment; it does not establish a
      CPU or SSD treatment effect. During the first 431-second setup, VM CPU
      averaged 0.95 cores and disk busy time 73%.<//
    >
    <${Table.ScrollContainer} minWidth=${650} type="native">
      <${Table} striped verticalSpacing="sm">
        <${Table.Caption}
          >Physical command intervals, 9 November UTC; reused receiving results
          are counted once.<//
        >
        <${Table.Thead}
          ><${Table.Tr}
            >${["Start / receipt", "Command", "Mean cores / 4", "Mean disk busy", "Mean memory full PSI"].map((value) => html`<${Table.Th} key=${value}>${value}<//>`)}<//
          ><//
        >
        <${Table.Tbody}
          >${snapshot.physical_runs.map(
            (run) =>
              html`<${Table.Tr} key=${run.start_utc}>
                <${Table.Td}
                  ><${Anchor} href=${invocation(run.receiving_invocation)}
                    >${run.start_utc.slice(11, 19)}<//
                  ><//
                >
                <${Table.Td}>${fixed(run.command_seconds, 1)} s<//>
                <${Table.Td}>${fixed(run.metrics.vm_cpu_cores.mean)}<//>
                <${Table.Td}
                  >${fixed(run.metrics.work_disk_busy_fraction.mean * 100, 1)}%<//
                >
                <${Table.Td}
                  >${fixed(run.metrics.host_memory_stall_fraction.mean * 100)}%<//
                >
              <//>`,
          )}<//
        >
      <//>
    <//>
    <${Text} size="sm" c="dimmed"
      >The first originating source is verified at ac0942ea. The second
      receiving result was reused/cached, so its originating source stays
      unconfirmed. Other cached summaries and epoch timestamps are excluded.
      These two runs do not support a representative failure/outcome
      regression.<//
    >
    <${NativeSelect}
      id="resource-window"
      label="Timeline window (UTC)"
      value=${window}
      data=${[
        { value: "runs", label: "Two correlated runs: Nov 9, 00:35–03:20" },
        { value: "day", label: "Complete 24-hour telemetry window" },
      ]}
      onChange=${(event) => setWindow(event.currentTarget.value)}
    />
    <${SimpleGrid} cols=${{ base: 1, md: 2 }} spacing="lg">
      ${charts.map(
        (chart) =>
          html`<${Paper}
            key=${chart.id}
            withBorder
            p="md"
            radius="md"
            data-line-chart=${chart.id}
          >
            <${Title} order=${3} size="h5" mb="md">${chart.title}<//>
            <${LineChart}
              h=${280}
              data=${timeline}
              dataKey="time"
              series=${chart.series}
              withDots=${false}
              withLegend
              curveType="linear"
              connectNulls=${false}
              xAxisProps=${{ interval: "preserveStartEnd", minTickGap: 40, tickMargin: 8 }}
              yAxisProps=${{ domain: chart.domain, width: 42 }}
              valueFormatter=${(value) => fixed(value, 1)}
            />
          <//>`,
      )}
    <//>
    <${Text} size="sm" c="dimmed"
      >Disk metrics cover the shared host filesystem, including other workloads.
      VM compute-container metrics include hypervisor/guest overhead. Pressure
      causes overlap and must not be added. The VM host cgroup forbids swap
      (memory.swap.max=0), so zero VM swap is expected policy; its resident RAM
      still contributes to host pressure.<//
    >
    <${Anchor} href="resource-timeline.png" download="resource-timeline.png"
      >Download the reviewed annotated timeline with physical run bands<//
    >
    <${Title} order=${3}>RAM and current host paging<//>
    <${Text}
      >The live guest had 13.04 GiB usable memory and 12.37 GiB disk cache, with
      zero guest swap counters. Its roughly 15.3 GiB QEMU RSS is not an action
      peak. Across 250 warm-control guest samples, MemAvailable stayed above
      11.86 GiB and sar used-memory peaked at 3.19 GiB. The sampled action
      cgroup highwater was 7.65 GiB including reclaimable cache. These measures
      have different scopes; no smaller-RAM intervention was tested. Adding RAM
      is not justified by these samples; a safe reduction is also unproven.
      Initial host zram occupancy was about 35 GiB logical versus 7.4 GiB
      physical compressed storage—different accounting layers.<//
    >
    <${Table.ScrollContainer} minWidth=${500} type="native">
      <${Table} striped verticalSpacing="sm">
        <${Table.Caption}
          >Readable selected processes, sequential snapshot around 10:02–10:05
          UTC. PSS apportions shared pages; process exits changed counts.<//
        >
        <${Table.Thead}
          ><${Table.Tr}
            >${["Family", "Processes", "Resident PSS GiB", "Swap PSS GiB"].map((value) => html`<${Table.Th} key=${value}>${value}<//>`)}<//
          ><//
        >
        <${Table.Tbody}
          >${snapshot.host_memory_snapshot.families.map((row) => html`<${Table.Tr} key=${row.family}><${Table.Td}>${row.family}<//><${Table.Td}>${row.readable_processes}<//><${Table.Td}>${fixed(row.resident_pss_gib)}<//><${Table.Td}>${fixed(row.swap_pss_gib)}<//><//>`)}<//
        >
      <//>
    <//>
    <${Text}
      >A ten-second pidstat sample at 10:08 UTC showed about 779 major faults/s
      in tmux and 434/829 in two Claude processes; QEMU averaged 0.1. Host
      counters confirmed active swap-ins. Major faults also include file-backed
      reads. The large rust-analyzer was idle. Visible tmux history was only
      about 55 MiB, so its excess retained allocation remains a separate
      investigation; no leak is established and no process was stopped.<//
    >
    <${Title} order=${3}
      >Real-KVM fixture comparison on the unpinned HIL canary<//
    >
    <${Text}
      >Kubernetes selected ovh-ns1001419 from zone hil-ovh; there is no hostname
      pin. One guest has four vCPUs, 16 GiB RAM, one action slot and 80 GiB NVMe
      scratch with an explicit 80 GiB storage request. Production routing and
      workers are unchanged. Both the nested-KVM smoke and full real
      guest/lifecycle scenario passed.<//
    >
    <${Paper} withBorder p="md" radius="md" data-chart="resource-fixture-pair">
      <${Title} order=${4} mb="md"
        >Same guest: baseline → candidate → warm baseline (seconds)<//
      >
      <${BarChart}
        h=${280}
        data=${pairData}
        dataKey="phase"
        withLegend
        series=${[
          { name: "Baseline", color: "blue.6" },
          { name: "Candidate", color: "teal.6" },
          { name: "Warm control", color: "orange.6" },
        ]}
        valueFormatter=${(value) => `${fixed(value, 1)} s`}
        xAxisProps=${{ interval: 0, tickMargin: 8 }}
        yAxisProps=${{ width: 45 }}
      />
    <//>
    <${Table.ScrollContainer} minWidth=${500} type="native">
      <${Table} striped verticalSpacing="sm">
        <${Table.Thead}
          ><${Table.Tr}
            ><${Table.Th}>Arm<//><${Table.Th}>Source / passed invocation<//
            ><${Table.Th}>Command<//><${Table.Th}>Input fetch<//
            ><${Table.Th}>Observed memory highwater<//><//
          ><//
        >
        <${Table.Tbody}
          >${["baseline", "candidate", "warm_baseline_control"].map(
            (arm) =>
              html`<${Table.Tr} key=${arm}
                ><${Table.Td}
                  >${arm === "warm_baseline_control" ? "Warm baseline" : arm === "baseline" ? "Baseline" : "Candidate"}<//
                ><${Table.Td}
                  ><${Anchor} href=${invocation(pair[arm].invocation)}
                    >${pair[arm].source.slice(0, 10)} · PASSED<//
                  ><//
                ><${Table.Td}>${fixed(pair[arm].command_seconds, 1)} s<//
                ><${Table.Td}>${fixed(pair[arm].input_fetch_seconds, 2)} s<//
                ><${Table.Td}
                  >${fixed(pair[arm].observed_memory_highwater_gib)} GiB<//
                ><//
              >`,
          )}<//
        >
      <//>
    <//>
    <${Blockquote}
      >The candidate tree is the baseline plus the fixture trim and shared
      canary routing. Observed command time fell 278.3 → 214.1 s, but baseline
      ran first: input fetch fell 12.28 → 0.98 s and the scenario itself varied
      by 19.56 s. Imports fell 26 → 20 and 69.45 → 37.94 s. Do not attribute the
      full 64.23 s difference to fixture removal. Setup contains imports, so
      those bars are not additive. The warm baseline then passed in 250.3 s:
      setup stayed at 120.2 s and imports at 67.50 s, while its scenario took
      95.37 s. Candidate setup remains about 43.4 s lower despite the warm
      control, and its scenario was slower than that control. This strengthens
      the setup signal; one candidate and fixed ordering still limit
      generalization.<//
    >
    <${Text}
      >One candidate action below 240 seconds does not demonstrate mean complete
      required-check CI below four minutes. The earlier 762-second wyrm2 run
      also differs in CPU family, storage, source and time; its difference from
      HIL is not an isolated SSD effect. A same-host HDD/SSD comparison was
      unavailable: wyrm2 SSD capacity was insufficient, and eligible HIL
      destinations have NVMe.<//
    >
    <${Text}
      ><${Anchor} href=${pair.pr}>Fixture trim PR #15099<//> merged at 10:21:37
      UTC on 9 November. The measurements above use the explicit experimental
      trees; post-merge complete-CI impact remains unmeasured. Ordinary PR-head
      CI was canceled, while focused and execution-contract checks passed.<//
    >
    <${Title} order=${3}>Current decisions<//>
    <${Text}
      >Finish same-source four/eight-vCPU trials and fixture repetitions as
      needed; the first warm baseline control passed. Keep one action per guest
      and retain 16 GiB while evaluating action peaks. Keep KEDA scale-in behind
      tested admission/drain ownership; defer a cache proxy given the historical
      1.6-second input-fetch stage.<//
    >
    <${Text}
      ><${Anchor} href=${snapshot.import_batching.pr}>Image-import PR #15126<//>
      remains draft with auto-merge disabled pending measured benefit. Its 132 →
      46 s preliminary import-span difference used different VMs. The
      eight-image/256 MiB policy was not empirically justified; a controlled
      comparison now includes serial, bulk per lifecycle group, all-at-start
      bulk and bounded imports. Keep the simplest measured design. Loading all
      groups first can sacrifice existing readiness overlap, and streaming means
      the byte cap does not directly bound resident blob memory.<//
    >
    <${Text} size="sm" c="dimmed"
      >Full UTC samples, PromQL, sources, physical clocks, experiment commands
      and limitations are in
      <${Anchor} href="evidence.json"
        >evidence.json → resource_telemetry_followup<//
      >. Process IDs, private addresses, command lines, workspace names, raw
      logs and worker identities remain outside publication.<//
    >
  <//>`;
}

export function ResourceFollowup() {
  const [snapshot, setSnapshot] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    const controller = new AbortController();
    fetch("evidence.json", { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error(`Evidence HTTP ${response.status}`);
        return response.json();
      })
      .then((evidence) => {
        if (!evidence.resource_telemetry_followup)
          throw new Error("Resource evidence missing");
        setSnapshot(evidence.resource_telemetry_followup);
      })
      .catch((reason) => {
        if (reason.name !== "AbortError") setError(reason.message);
      });
    return () => controller.abort();
  }, []);
  return html`<${Stack}
    id="resource-followup"
    component="section"
    my="xl"
    gap="md"
  >
    <${Title} order=${2}>Resource telemetry and new real-KVM evidence<//>
    ${error ? html`<${Text} c="red">Resource evidence could not load: ${error}<//>` : snapshot ? html`<${ResourceContent} snapshot=${snapshot} />` : html`<${Text}>Loading resource evidence…<//>`}
  <//>`;
}
