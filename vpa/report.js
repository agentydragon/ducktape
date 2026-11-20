import {
  Accordion,
  Alert,
  Anchor,
  Badge,
  Box,
  Button,
  Code,
  Container,
  Group,
  NativeSelect,
  Paper,
  SimpleGrid,
  Stack,
  Table,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { BarChart } from "@mantine/charts";
import { useEffect, useState } from "react";
import { html, mount } from "../report.js";

const source =
  "https://github.com/agentydragon/ducktape/blob/05a4c55db085311363fad8cce0ba4dfe3f4ecb0f/";
const github = "https://github.com/agentydragon/ducktape/";
const currentSource = github + "blob/7514082e13cff17faa64056a67b285c7e259f3e2/";
const modeLabels = {
  Off: "Recommend",
  Initial: "Initial",
  Recreate: "Recreate",
};

function quantityMiB(value) {
  if (value == null) return "—";
  const match = String(value).match(/^([0-9.]+)(Ki|Mi|Gi|[kMG])?$/);
  if (!match) return String(value);
  const scale = {
    Ki: 1 / 1024,
    Mi: 1,
    Gi: 1024,
    k: 1000 / 1048576,
    M: 1000000 / 1048576,
    G: 1000000000 / 1048576,
  };
  return `${Math.round(Number(match[1]) * (scale[match[2]] ?? 1 / 1048576))} MiB`;
}

function mode(row) {
  return row.vpas.length
    ? (modeLabels[row.vpas[0].mode] ?? row.vpas[0].mode)
    : "No direct VPA";
}

function policy(row) {
  if (!row.vpas.length) return "No direct target";
  const policies = row.vpas[0].containers;
  if (!policies.length) return "Default: requests + limits";
  return policies
    .map(
      (p) =>
        `${p.containerName ?? "*"}: ${p.mode === "Off" ? "off" : (p.controlledValues ?? "requests + limits")}`,
    )
    .join("; ");
}

function Stat({ value, label, detail }) {
  return html`<${Paper} withBorder p="md" radius="md">
    <${Text} size="xl" fw=${700}>${value}<//>
    <${Text} fw=${600}>${label}<//>
    <${Text} size="sm" c="dimmed">${detail}<//>
  <//>`;
}

function Inventory({ workloads }) {
  const [search, setSearch] = useState("");
  const [chosenMode, setChosenMode] = useState("All");
  const [missing, setMissing] = useState("All");
  const [shown, setShown] = useState(30);
  const query = search.trim().toLowerCase();
  const filtered = workloads.filter((row) => {
    const text =
      `${row.namespace} ${row.name} ${row.kind} ${row.containers.map((c) => c.name).join(" ")}`.toLowerCase();
    const requestMissing = row.containers.some(
      (c) => c.requests.memory == null || c.requests.cpu == null,
    );
    const limitMissing = row.containers.some(
      (c) => c.limits.memory == null || c.limits.cpu == null,
    );
    return (
      (!query || text.includes(query)) &&
      (chosenMode === "All" || mode(row) === chosenMode) &&
      (missing === "All" ||
        (missing === "Missing request" && requestMissing) ||
        (missing === "Missing limit" && limitMissing))
    );
  });
  const setFilter = (fn) => (event) => {
    fn(event.currentTarget.value);
    setShown(30);
  };
  return html`<${Box}>
    <${SimpleGrid} cols=${{ base: 1, sm: 3 }} spacing="md" mb="md">
      <${TextInput}
        label="Search namespace, workload or container"
        type="search"
        value=${search}
        onChange=${setFilter(setSearch)}
      />
      <${NativeSelect}
        label="VPA mode"
        value=${chosenMode}
        onChange=${setFilter(setChosenMode)}
        data=${["All", "No direct VPA", "Recommend", "Initial", "Recreate"]}
      />
      <${NativeSelect}
        label="Template resource fields"
        value=${missing}
        onChange=${setFilter(setMissing)}
        data=${["All", "Missing request", "Missing limit"]}
      />
    <//>
    <${Text} size="sm" c="dimmed" aria-live="polite"
      >${`Showing ${Math.min(shown, filtered.length)} of ${filtered.length} matching controller templates. Omitted fields are labeled unset; they are not zero usage.`}<//
    >
    <${Table.ScrollContainer}
      minWidth=${1040}
      type="native"
      tabIndex=${0}
      aria-label="Workload VPA inventory"
    >
      <${Table} striped highlightOnHover withTableBorder verticalSpacing="sm">
        <${Table.Thead}
          ><${Table.Tr}>
            <${Table.Th}>Namespace / workload<//><${Table.Th}>VPA policy<//
            ><${Table.Th}>Template requests / limits<//>
            <${Table.Th}>Live memory and 7-day peak<//
            ><${Table.Th}>Recommendation / HPA<//>
          <//><//
        >
        <${Table.Tbody}>
          ${filtered.slice(0, shown).map(
            (row) =>
              html`<${Table.Tr}
                key=${`${row.namespace}/${row.kind}/${row.name}`}
              >
                <${Table.Td}
                  ><${Text} fw=${600} size="sm"
                    >${row.namespace} / ${row.name}<//
                  ><${Text} size="xs" c="dimmed"
                    >${row.kind} · ${row.activePods} active
                    pod(s)${row.desiredReplicas == null ? "" : ` · ${row.readyReplicas ?? 0}/${row.desiredReplicas} ready`}<//
                  ><//
                >
                <${Table.Td}
                  ><${Badge}
                    variant="light"
                    color=${mode(row) === "No direct VPA" ? "gray" : mode(row) === "Recommend" ? "yellow" : "blue"}
                    >${mode(row)}<//
                  ><${Text} size="xs" mt="xs">${policy(row)}<//><//
                >
                <${Table.Td}
                  >${row.containers.map((c) => html`<${Box} key=${c.name} mb="xs"><${Text} size="xs" fw=${600}>${c.name}<//><${Text} size="xs">req ${c.requests.cpu ?? "unset"} CPU / ${c.requests.memory ?? "unset"} memory<//><${Text} size="xs">limit ${c.limits.cpu ?? "unset"} CPU / ${c.limits.memory ?? "unset"} memory<//><//>`)}<//
                >
                <${Table.Td}
                  >${row.containers.map((c) => html`<${Box} key=${c.name} mb="xs"><${Text} size="xs" fw=${600}>${c.name}<//><${Text} size="xs">now ${c.currentMemoryMaxMiB == null ? "unknown" : `${c.currentMemoryMaxMiB} MiB`} · peak ${c.peak7dCurrentPodMiB == null ? "unknown" : `${c.peak7dCurrentPodMiB} MiB`}<//><${Text} size="xs" c="dimmed">live request: ${c.liveMemoryRequests.length ? c.liveMemoryRequests.join(", ") : "no active pod"}<//><//>`)}<//
                >
                <${Table.Td}
                  >${row.vpas.flatMap((v) => v.recommendations).map((v) => html`<${Text} size="xs" key=${v.containerName}>${v.containerName}: ${v.target?.cpu ?? "—"} CPU / ${quantityMiB(v.target?.memory)}<//>`)}${row.hpas.map((h) => html`<${Text} size="xs" c="orange" key=${h.name}>HPA: ${h.name}<//>`)}${!row.vpas.length && !row.hpas.length ? html`<${Text} size="xs" c="dimmed">No direct VPA<//>` : null}<//
                >
              <//>`,
          )}
        <//>
      <//>
    <//>
    ${shown < filtered.length ? html`<${Button} variant="light" mt="md" onClick=${() => setShown(shown + 50)}>Show 50 more<//>` : null}
  <//>`;
}

function Report() {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    fetch("./evidence.json?v=current-20261120-2030")
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(setData)
      .catch((e) => setError(String(e)));
  }, []);
  useEffect(() => {
    if (data && window.location.hash)
      document.getElementById(window.location.hash.slice(1))?.scrollIntoView();
  }, [data]);
  if (error)
    return html`<${Container} py="xl"
      ><${Alert} color="red" title="Evidence could not be loaded"
        >${error}<//
      ><//
    >`;
  if (!data)
    return html`<${Container} py="xl"><${Text}>Loading VPA evidence…<//><//>`;
  const current = data.current_state;
  const chart = Object.entries(current.vpa_modes).map(([mode, count]) => ({
    mode,
    count,
  }));
  return html`<${Container} size="xl" py="xl">
    <${Anchor} href="../">← All reports<//>
    <${Title} order=${1} mt="md">Cluster VPA coverage and action plan<//>
    <${Text} size="lg" c="dimmed" mt="sm"
      >A dated view of resource sizing, availability, and the next useful
      batches.<//
    >
    <${Text} size="sm" c="dimmed"
      >Cluster observations: 20 November 2026, 20:28–20:30 UTC. This page is a
      snapshot, not a live dashboard.<//
    >
    <${Group} gap="md" my="md"
      ><${Anchor} href="#current-state">Current state<//
      ><${Anchor} href="#remaining-work">Remaining work<//
      ><${Anchor} href="#inventory">Historical inventory<//
      ><${Anchor} href="#sources">Sources<//><//
    >
    <section id="current-state">
      <span id="coverage"></span><span id="adoption-progress"></span
      ><span id="verified-rollouts"></span><span id="rollout-follow-up"></span
      ><span id="rollout-wave"></span><span id="architecture"></span>
      <${Title} order=${2} mt="xl">Current state<//>
      <${SimpleGrid} cols=${{ base: 1, sm: 3 }} spacing="md" my="md">
        <${Stat}
          value="192 / 233"
          label="Active controllers with a direct VPA"
          detail="Deployments, StatefulSets and DaemonSets; 41 have no direct target."
        />
        <${Stat}
          value="226"
          label="VPA objects"
          detail="Includes targets other than active controllers."
        />
        <${Stat}
          value="10"
          label="Staging service VPAs"
          detail="Initial, memory requests only; every one has a recommendation."
        />
      <//>
      <${SimpleGrid} cols=${{ base: 1, md: 2 }} spacing="md">
        <${Paper} withBorder p="md" radius="md">
          <${Title} order=${3} size="h4">VPA objects by update mode<//>
          <${BarChart}
            h=${220}
            data=${chart}
            dataKey="mode"
            series=${[{ name: "count", color: "blue.6" }]}
            tickLine="none"
            gridAxis="y"
          />
          <${Text} size="sm" c="dimmed"
            >105 Initial · 67 Off · 54 Recreate. These are objects, not the
            number of active controllers covered; Jobs and CronJobs are
            included.<//
          >
        <//>
        <${Paper} withBorder p="md" radius="md">
          <${Title} order=${3} size="h4">How to read the modes<//>
          <${Text} component="p"
            ><strong>Off</strong> recommends without changing Pods.${" "}
            <strong>Initial</strong> applies at creation without VPA
            eviction.${" "} <strong>Recreate</strong> can evict running Pods to
            apply recommendations.<//
          >
          <${Text} component="p"
            >The effective per-container policy determines whether requests,
            limits, or both are controlled. Preserve existing limit ownership
            when extending a VPA.<//
          >
          <${Text} component="p" mb=${0}
            >All 41 active controllers without a direct VPA are in namespaces
            explicitly disabling Goldilocks; this is an audit queue, not an
            automatic enablement list.<//
          >
        <//>
      <//>
      <${Title} order=${3} mt="xl">Availability and policy guardrails<//>
      <${SimpleGrid} cols=${{ base: 1, md: 2 }} spacing="md" mt="md">
        <${Paper} id="controller-ha" withBorder p="md" radius="md"
          ><span id="web-frontend-ha"></span
          ><${Title} order=${4}>Replicas and placement<//>
          <${Text} component="p"
            >Crossplane has three Ready webhook endpoints, one each in home-lan,
            atlas and hil-ovh, with a PDB allowing one disruption. Website and
            Haku static each have two Ready replicas on different hosts and PDBs
            allowing one disruption. Metrics-server is also 2/2 Ready.<//
          >
          <${Text} component="p" mb=${0}
            >Website and Haku use revision-scoped hostname spread with
            minDomains 2: a second replica stays Pending when only one host is
            eligible. Host spread does not guarantee zone spread. These checks
            are not fault injection or a capacity guarantee.<//
          >
        <//>
        <${Paper} id="agentplane-staging-initial" withBorder p="md" radius="md"
          ><${Title} order=${4}>Singletons and staging<//>
          <${Text} component="p"
            >Staging has ten explicitly targeted Initial VPAs while
            namespace-wide Goldilocks remains disabled for dynamic Sandboxes.
            The two newest targets were tested with server-side admission dry
            runs; no natural replacement or scheduling has been proved for those
            changes.<//
          >
          <${Text} component="p" mb=${0}
            >Loki read-proxy is Initial with a 128–160 MiB memory request range
            and a 50m CPU pin. Its target is 128 MiB; an owner-matched admission
            dry run applied 50m/128 MiB and preserved the 200m/256 MiB limits.
            Its existing singleton remains Ready and has not been replaced. Ntfy
            and CDviz also need a non-evicting approach while singletons.<//
          >
        <//>
        <${Paper}
          id="secret-store-background-recovery"
          withBorder
          p="md"
          radius="md"
          ><span id="kyverno-retirement"></span
          ><${Title} order=${4}>Policy reporting<//>
          <${Text} component="p" mb=${0}
            >Kyverno remains in use where it is the simpler owner. Its
            SecretStore validating policy now matches the sole served API
            version, v1. At 20:16 UTC, all 60 current ClusterSecretStore UIDs
            had fresh background pass reports after the version fix. A separate
            native admission policy enforced the tested unfenced-store
            rejection.<//
          >
        <//>
        <${Paper}
          id="clickhouse-exporter-initial"
          withBorder
          p="md"
          radius="md"
        >
          <${Title} order=${4}>ClickHouse operator<//>
          <${Text} component="p" mb=${0}
            >The operator exporter has an Initial VPA for memory requests,
            bounded to 96–128 MiB. The operator container is Off with request
            pins to protect against stale recommendations; the ClickHouse and
            Keeper StatefulSet VPAs remain Off. A server-side admission dry run
            verified the pins and limits, but no replacement Pod has proved
            natural scheduling.<//
          >
        <//>
        <${Paper} withBorder p="md" radius="md"
          ><${Title} order=${4}>Resource evidence<//>
          <${Text} component="p" mb=${0}
            >At 20:34 UTC, 132 of 578 regular containers in Running or Pending,
            non-terminating Pods lacked both CPU and memory requests; 375 lacked
            CPU limits and 254 lacked memory limits. These are admitted Pod
            specs, including defaults and mutations, and exclude init and
            ephemeral containers. Missing fields alone do not show usage or an
            OOM cause.<//
          >
          <${Text} component="p" mb=${0}
            >The largest request gaps are Piraeus (63 containers) and SeaweedFS
            CSI (16); another 42 PostgreSQL containers belong to 21 CNPG
            clusters. Work through those with their operator owners. The earlier
            02:58 inventory remains available below as historical evidence.<//
          >
        <//>
      <//>
    </section>
    <section id="remaining-work">
      <span id="batches"></span>
      <${Title} order=${2} mt="xl">Remaining work<//>
      <${Stack} gap="md" mt="md">
        <${Paper} withBorder p="md" radius="md"
          ><${Title} order=${3} size="h4"
            >1. Expand coverage in useful batches<//
          ><${Text} component="p" mb=${0}
            >Review the 41 uncovered active controllers by owner and workload
            class. Prioritize safe static, replicated services and explicit
            bounded memory requests; check startup usage, template floors, HPA
            interactions, placement and the actual VPA policy. Treat
            Goldilocks-disabled operator and dynamic workloads individually.
            Refresh counts and recommendations before choosing each batch.<//
          ><//
        >
        <${Paper} withBorder p="md" radius="md"
          ><${Title} order=${3} size="h4"
            >2. Use eviction where availability supports it<//
          ><${Text} component="p" mb=${0}
            >Move restart-safe replicas toward Recreate in cohorts after
            checking PDBs, endpoints, spread, capacity and rollout behavior.
            Keep singletons such as ntfy and CDviz in Initial until replacement
            is safe. Preserve VPAs already managing both requests and limits; do
            not silently weaken their policies. Storage work has a separate
            owner.<//
          ><//
        >
        <${Paper} withBorder p="md" radius="md"
          ><${Title} order=${3} size="h4"
            >3. Check admission and capacity on natural rollouts<//
          ><${Text} component="p" mb=${0}
            >Initial does not restart existing Pods. Check newly created Pods
            against their controller and intended VPA policy, including the
            known owner-cache race; keep safe native template floors for
            admission misses. Compare bounded requests and concurrent surge
            against node headroom: the earlier GitLab Sidekiq model showed an
            adverse overlap could block its surge. A blocked surge can stall a
            rollout even when the incumbent stays available.<//
          ><//
        >
        <${Paper} withBorder p="md" radius="md"
          ><${Title} order=${3} size="h4">4. Keep policy reporting effective<//
          ><${Text} component="p" mb=${0}
            >Observe the next recurring SecretStore background scan and keep
            coverage across all current store UIDs. Continue supported Kyverno
            policy API migrations with their own behavior and reporting checks.
            Replace Kyverno only for a specific case where the alternative is
            simpler and preserves the required behavior; building a separate
            scanner is not that simplification.<//
          ><//
        >
      <//>
    </section>
    <section id="inventory">
      <${Title} order=${2} mt="xl">Historical inventory<//>
      <${Text} component="p"
        >The searchable inventory is an archival snapshot of 237 controller
        templates at 02:58 UTC on 20 November, with seven-day queries ending at
        03:03 UTC. Names, policies, recommendations and resources may have
        changed; it is not the current 233-active-controller census.<//
      >
      <${Accordion} variant="separated"
        ><${Accordion.Item} value="inventory"
          ><${Accordion.Control}>Open the 02:58 UTC inventory<//
          ><${Accordion.Panel}
            ><${Inventory} workloads=${data.workloads} /><//><//
      ><//>
    </section>
    <section id="sources">
      <${Title} order=${2} mt="xl">Sources and limits<//>
      <${Text} component="p"
        >The current census and placement were observed at 20:28–20:30 UTC. The
        Flux source was ${" "}<${Anchor} href=${currentSource}
          >devel at 7514082e13<//
        >${" "}; Git source and observed running state are distinct. The initial
        ${" "}<${Anchor} href=${source}>02:58 source revision<//>${" "} and
        historical metrics are retained in ${" "}<${Anchor}
          href="./evidence.json"
          >evidence.json<//
        >${" "}; hashes and timestamps are in ${" "}<${Anchor}
          href="./manifest.json"
          >manifest.json<//
        >.<//
      >
      <${Text} component="p"
        >Supporting changes: ${" "}<${Anchor} href=${github + "pull/18245"}
          >staging targets<//
        >, ${" "}<${Anchor} href=${github + "pull/18249"}
          >SecretStore scan repair<//
        >, ${" "}<${Anchor} href=${github + "pull/18253"}
          >Crossplane site spread<//
        >, ${" "}<${Anchor} href=${github + "pull/18264"}>Loki proxy VPA<//>,
        ${" "}<${Anchor} href=${github + "pull/18210"}>website spread<//>${" "}
        and ${" "}<${Anchor} href=${github + "pull/18211"}>Haku spread<//>.
        Recommendations and dry runs are evidence of configuration, not proof of
        future admission, scheduling, authenticated actions or host-failure
        recovery.<//
      >
    </section>
  <//>`;
}

mount(Report);
