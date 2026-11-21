import {
  Alert,
  Anchor,
  Badge,
  Box,
  Button,
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
const currentSource = github + "blob/a78ec26f653c73377901bb4b950070f990e51e4f/";
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
  const isMissing = (x) => x == null || Number.parseFloat(x) === 0;
  const filtered = workloads.filter((row) => {
    const text =
      `${row.namespace} ${row.name} ${row.kind} ${row.containers.map((c) => `${c.name} ${c.admitted.map((p) => p.pod).join(" ")}`).join(" ")}`.toLowerCase();
    const values = row.containers.flatMap((c) => c.admitted);
    const templates = row.containers.flatMap((c) =>
      c.template ? [c.template] : [],
    );
    const resourceMissing = (items, field) =>
      items.some((p) => isMissing(p[field].cpu) || isMissing(p[field].memory));
    return (
      (!query || text.includes(query)) &&
      (chosenMode === "All" || mode(row) === chosenMode) &&
      (missing === "All" ||
        (missing === "Pod missing request" &&
          resourceMissing(values, "requests")) ||
        (missing === "Pod missing limit" &&
          resourceMissing(values, "limits")) ||
        (missing === "Template missing request" &&
          resourceMissing(templates, "requests")) ||
        (missing === "Template missing limit" &&
          resourceMissing(templates, "limits")))
    );
  });
  const setFilter = (fn) => (event) => {
    fn(event.currentTarget.value);
    setShown(30);
  };
  const fmt = (r) => `${r.cpu ?? "unset"} CPU / ${r.memory ?? "unset"} memory`;
  return html`<${Box}>
    <${SimpleGrid} cols=${{ base: 1, sm: 3 }} spacing="md" mb="md">
      <${TextInput}
        label="Search namespace, workload, Pod or container"
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
        label="Resource field filter"
        value=${missing}
        onChange=${setFilter(setMissing)}
        data=${["All", "Pod missing request", "Pod missing limit", "Template missing request", "Template missing limit"]}
      />
    <//>
    <${Text} size="sm" c="dimmed" aria-live="polite"
      >${`Showing ${Math.min(shown, filtered.length)} of ${filtered.length} matching rows. A missing filter matches either CPU or memory absent or zero. Filters distinguish templates from admitted active Pods; owner-only rows have no template and are not proof of direct VPA coverage.`}<//
    >
    <${Table.ScrollContainer}
      minWidth=${1160}
      type="native"
      tabIndex=${0}
      aria-label="Current workload VPA inventory"
    >
      <${Table} striped highlightOnHover withTableBorder verticalSpacing="sm">
        <${Table.Thead}
          ><${Table.Tr}
            ><${Table.Th}>Namespace / owner<//><${Table.Th}>Direct VPA policy<//
            ><${Table.Th}>Template and admitted resources<//
            ><${Table.Th}>Memory observations<//
            ><${Table.Th}>VPA recommendation / HPA<//><//
          ><//
        >
        <${Table.Tbody}>
          ${filtered.slice(0, shown).map(
            (row) =>
              html`<${Table.Tr}
                key=${`${row.apiGroup}/${row.namespace}/${row.kind}/${row.name}`}
              >
                <${Table.Td}
                  ><${Text} fw=${600} size="sm"
                    >${row.namespace} / ${row.name}<//
                  ><${Text} size="xs" c="dimmed"
                    >${`${row.kind} · ${row.scope === "pod-owner" ? "Pod owner; no template" : "controller template"} · ${row.activePods} active Pod(s)${row.desiredReplicas == null ? "" : ` · ${row.readyReplicas}/${row.desiredReplicas} ready`}`}<//
                  ><//
                >
                <${Table.Td}
                  >${mode(row) === "No direct VPA" ? html`<${Text} size="xs" c="dimmed">No direct VPA<//>` : html`<${Badge} variant="light" color=${mode(row) === "Recommend" ? "yellow" : "blue"}>${mode(row)}<//>`}${row.vpas.map((v) => html`<${Box} key=${v.name} mt="xs"><${Text} size="xs" fw=${600}>${v.name} · ${v.mode}<//>${v.containers.length ? v.containers.map((c) => html`<${Text} size="xs" key=${c.containerName}>${`${c.containerName ?? "*"}: ${c.mode === "Off" ? "off" : `${(c.controlledResources ?? ["cpu", "memory"]).join(", ")} · ${c.controlledValues ?? "RequestsAndLimits"}`}${c.minAllowed ? ` · min ${fmt(c.minAllowed)}` : ""}${c.maxAllowed ? ` · max ${fmt(c.maxAllowed)}` : ""}`}<//>`) : html`<${Text} size="xs">Default: CPU and memory requests and limits<//>`}<//>`)}<//
                >
                <${Table.Td}
                  >${row.containers.map(
                    (c) =>
                      html`<${Box} key=${c.name} mb="xs"
                        ><${Text} size="xs" fw=${600}>${c.name}<//
                        ><${Text} size="xs"
                          >${c.template ? `template req ${fmt(c.template.requests)}; limit ${fmt(c.template.limits)}` : "Template unavailable for this owner"}<//
                        >${
                          c.admitted.length
                            ? html`<details>
                                <summary>
                                  ${`${c.admitted.length} admitted Pod container(s)`}
                                </summary>
                                ${c.admitted.map((a) => html`<${Text} size="xs" key=${a.pod}>${`${a.pod} · ${a.phase} · ${a.node ?? "unscheduled"}: req ${fmt(a.requests)}; limit ${fmt(a.limits)}`}<//>`)}
                              </details>`
                            : html`<${Text} size="xs" c="dimmed"
                                >No active Pod container<//
                              >`
                        }<//
                      >`,
                  )}<//
                >
                <${Table.Td}
                  >${row.containers.map((c) => html`<${Box} key=${c.name} mb="xs"><${Text} size="xs" fw=${600}>${c.name}<//><${Text} size="xs">${`current max ${c.currentMemoryMaxMiB == null ? "unknown" : `${c.currentMemoryMaxMiB} MiB`} (${c.sampledPods}/${c.admitted.length} Pods)`}<//><${Text} size="xs">${`7-day max ${c.peak7dCurrentPodMiB == null ? "unknown" : `${c.peak7dCurrentPodMiB} MiB`} (${c.peakSampledPods}/${c.admitted.length} current Pods)`}<//><//>`)}<//
                >
                <${Table.Td}
                  >${row.vpas.flatMap((v) => v.recommendations.map((c) => html`<${Text} size="xs" key=${`${v.name}/${c.containerName}`}>${`${c.containerName}: ${c.target?.cpu ?? "unknown"} CPU / ${quantityMiB(c.target?.memory)}`}<//>`))}${row.hpas.map((h) => html`<${Text} size="xs" c="orange" key=${h.name}>HPA: ${h.name}<//>`)}${row.vpas.length && !row.vpas.some((v) => v.recommendations.length) ? html`<${Text} size="xs">Recommendation unavailable<//>` : null}<//
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
    Promise.all(
      ["./evidence.json", "./inventory.json"].map((url) =>
        fetch(`${url}?v=20261120-2255`).then((r) => {
          if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
          return r.json();
        }),
      ),
    )
      .then(([evidence, inventory]) => ({ ...evidence, inventory }))
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
  const current = data.inventory;
  const counts = current.counts;
  const missingRequestContainers = (row) =>
    row.containers
      .flatMap((c) => c.admitted)
      .filter((p) => !p.requests.cpu || !p.requests.memory).length;
  const piraeusMissing = current.workloads
    .filter((row) => row.namespace === "piraeus-datastore")
    .reduce((n, row) => n + missingRequestContainers(row), 0);
  const seaweedMissing = current.workloads
    .filter((row) => row.namespace === "seaweedfs-csi-system")
    .reduce((n, row) => n + missingRequestContainers(row), 0);
  const cnpgWithGaps = current.workloads.filter(
    (row) =>
      row.apiGroup === "postgresql.cnpg.io" &&
      row.kind === "Cluster" &&
      missingRequestContainers(row) > 0,
  );
  const cnpgMissing = cnpgWithGaps.reduce(
    (n, row) => n + missingRequestContainers(row),
    0,
  );
  const chart = Object.entries(counts.vpa_modes).map(([mode, count]) => ({
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
      >${`Inventory collection: ${current.collection_started_utc} to ${current.collection_finished_utc}. Availability was observed at 20:28–20:30 UTC; SecretStore reporting and the availability checks below were observed at the times stated. This is a snapshot, not a live dashboard.`}<//
    >
    <${Group} gap="md" my="md"
      ><${Anchor} href="#current-state">Current state<//
      ><${Anchor} href="#remaining-work">Remaining work<//
      ><${Anchor} href="#inventory">Current inventory<//
      ><${Anchor} href="#sources">Sources<//><//
    >
    <section id="current-state">
      <span id="coverage"></span><span id="adoption-progress"></span
      ><span id="verified-rollouts"></span><span id="rollout-follow-up"></span
      ><span id="rollout-wave"></span><span id="architecture"></span>
      <${Title} order=${2} mt="xl">Current state<//>
      <${SimpleGrid} cols=${{ base: 1, sm: 3 }} spacing="md" my="md">
        <${Stat}
          value=${`${counts.active_direct_vpa} / ${counts.active_controllers}`}
          label="Active controllers with a direct VPA"
          detail=${`Active Deployments, StatefulSets and DaemonSets; ${counts.active_controllers - counts.active_direct_vpa} have no direct target.`}
        />
        <${Stat}
          value=${String(counts.all_vpa_objects)}
          label="VPA objects"
          detail="Includes targets other than active controllers."
        />
        <${Stat}
          value="11"
          label="Staging service VPAs"
          detail="Ten Initial, one Recreate; memory requests only."
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
            >${`${counts.vpa_modes.Initial ?? 0} Initial · ${counts.vpa_modes.Off ?? 0} Off · ${counts.vpa_modes.Recreate ?? 0} Recreate.`}${" "}
            These are objects, not the number of active controllers covered;
            Jobs and CronJobs are included.<//
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
            >${`The ${counts.active_controllers - counts.active_direct_vpa} active controllers without a direct VPA in this snapshot are in namespaces explicitly disabling Goldilocks; this is an audit queue, not an automatic enablement list.`}<//
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
          <${Text} component="p"
            >On 20 November, the ${" "}<${Anchor} href=${github + "pull/18294"}
              >cert-manager CA injector<//
            >, ${" "}<${Anchor} href=${github + "pull/18300"}
              >controller and webhook<//
            >, and ${" "}<${Anchor} href=${github + "pull/18319"}
              >External Secrets webhook<//
            >
            had multiple Ready replicas, PDBs permitting one disruption and
            Recreate VPAs. Webhook endpoint and admission checks passed; no
            eviction or leader handoff was tested.<//
          >
          <${Text} component="p"
            >At 22:36 UTC, all ten staging services in the ${" "}<${Anchor}
              href=${github + "pull/18302"}
              >spread cohort<//
            >${" "}had two updated, Ready replicas on distinct nodes and PDBs
            allowing one disruption. Frontend uses Recreate; the other nine
            remain Initial. Historical recovery suites cover selected services,
            but Pod readiness does not prove current Notifications delivery or
            uninterrupted in-flight work, and no deliberate node-failure test
            was performed.<//
          >
          <${Text} component="p"
            >At 03:45 UTC on 21 November, the ${" "}<${Anchor}
              href=${github + "pull/18382"}
              >kubeapi-proxy<//
            >${" "}VPA used Recreate after a namespace reconciliation trigger.
            Both replicas were Ready on separate hosts and its PDB allowed one
            disruption. Memory requests are bounded to 16–24 MiB; the VPA's
            uncapped recommendation was 100 MiB, while the sampled seven-day
            peak for the current Pods was 12.4 MiB. That peak excludes replaced
            Pods and does not establish a safe bound under all traffic. No
            eviction or authenticated endpoint check was performed here.<//
          >
          <${Text} component="p"
            >At 03:47 UTC on 21 November, both metrics-server replicas returned
            metrics for all nine Ready nodes and all 463 Ready Running Pods at
            least two minutes old when queried individually. Three eligible
            nodes without a replica each had at least 2.2 CPU and 3.4 GiB of
            scheduler-request headroom, versus a 15m CPU and approximately 121
            MiB memory request per replacement. This is a point-in-time capacity
            and endpoint check, not a failover test.<//
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
            >Staging has eleven explicitly targeted VPAs while namespace-wide
            Goldilocks remains disabled for dynamic Sandboxes. ${" "}<${Anchor}
              href=${github + "pull/18293"}
              >Electric<//
            >${" "}uses Initial with a 448–576 MiB memory request range; its 448
            MiB target was applied in an owner-backed admission dry run. A
            second Electric on the same stream can pass health checks while
            refusing new shapes, so VPA must not evict its singleton.<//
          >
          <${Text} component="p" mb=${0}
            >Ntfy and CDviz also need non-evicting policies while singletons.
            The inventory below shows their effective policies and admitted Pod
            resources; historical admission checks remain in the evidence.<//
          >
        <//>
        <${Paper} withBorder p="md" radius="md">
          <${Title} order=${4}>Platform Sessions providers<//>
          <${Text} component="p" mb=${0}
            >${" "}<${Anchor} href=${github + "pull/18282"}
              >ChatGPT and OpenAI providers<//
            >${" "} each have an Initial, memory-request-only VPA bounded to
            128–192 MiB. Owner-matched admission dry runs preserved the 50m CPU
            request and 1 GiB limit; the existing singleton Pods remained Ready
            and were not replaced.<//
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
            version, v1. At 21:16 UTC, a subsequent hourly background scan
            produced pass reports for all 60 current ClusterSecretStore UIDs. A
            separate native admission policy enforced the tested unfenced-store
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
            >${`In this inventory, ${counts.missing_requests_cpu} of ${counts.regular_containers} regular containers in Running or Pending, non-terminating Pods lacked CPU requests and ${counts.missing_requests_memory} lacked memory requests; ${counts.missing_limits_cpu} lacked CPU limits and ${counts.missing_limits_memory} lacked memory limits.`}${" "}
            These are admitted Pod specs, including defaults and mutations, and
            exclude init and ephemeral containers. Missing fields alone do not
            show usage or an OOM cause.<//
          >
          <${Text} component="p" mb=${0}
            >${`Request gaps include Piraeus (${piraeusMissing} containers) and SeaweedFS CSI (${seaweedMissing}); another ${cnpgMissing} PostgreSQL containers belong to ${cnpgWithGaps.length} CNPG clusters.`}${" "}
            Work through those with their operator owners. Search the current
            inventory below for individual owners and containers.<//
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
            >${`Review the ${counts.active_controllers - counts.active_direct_vpa} uncovered active controllers by owner and workload class.`}${" "}
            Prioritize safe static, replicated services and explicit bounded
            memory requests; check startup usage, template floors, HPA
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
            not silently weaken their policies. For example, metrics-server's
            current VPA controls both requests and limits and has no explicit
            ceiling; review proportional limit changes and capacity before
            enabling eviction. ${" "}<${Anchor} href=${github + "pull/18449"}
              >The proposed metrics-server change<//
            >
            preserves control of both. Check service-specific recovery and
            capacity before further promotion: PDBs do not guarantee
            uninterrupted in-flight work. Storage work has a separate owner.<//
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
            >Continue supported Kyverno policy API migrations with their own
            behavior and reporting checks. Replace Kyverno only for a specific
            case where the alternative is simpler and preserves the required
            behavior; building a separate scanner is not that simplification.<//
          ><//
        >
      <//>
    </section>
    <section id="inventory">
      <${Title} order=${2} mt="xl">Current searchable inventory<//>
      <${Text} component="p"
        >Each row is a Deployment, StatefulSet, DaemonSet or CronJob template,
        or an active Pod owner whose template is outside that set. Deployment
        attribution follows Pod → ReplicaSet → Deployment by UID; CronJobs
        follow Pod → Job → CronJob by UID. Custom-owner rows show admitted Pods
        only and do not imply that the owner supports VPA. VPA matching uses API
        group, kind, namespace and target name.<//
      >
      <${Text} component="p"
        >Template values come from stored controller templates, which may
        themselves be defaulted or mutated; admitted Pod values also include Pod
        admission and VPA mutation. Pod filters apply to actual non-terminating
        Running or Pending Pods and regular containers; template filters include
        inactive controllers. Current memory is from metrics-server; seven-day
        working-set maxima are sampled by Mimir for current Pod names, which may
        cover less than seven days or be reused. Unknown means no sample, not
        zero.<//
      >
      <${Inventory} workloads=${current.workloads} />
    </section>
    <section id="sources">
      <${Title} order=${2} mt="xl">Sources and limits<//>
      <${Text} component="p"
        >The census was collected during the inventory window above; placement
        was observed at 20:28–20:30 UTC except where stated. The Flux source at
        22:56 UTC was ${" "}<${Anchor} href=${currentSource}
          >devel at a78ec26f65<//
        >${" "}; Git source and observed running state are distinct. The initial
        ${" "}<${Anchor} href=${source}>02:58 source revision<//>${" "} and
        historical metrics are retained in ${" "}<${Anchor}
          href="./evidence.json"
          >historical evidence.json<//
        >${" "}; current inventory is in ${" "}<${Anchor}
          href="./inventory.json"
          >inventory.json<//
        >${" "}, with the ${" "}<${Anchor} href="./collect_inventory.py"
          >collector<//
        >${" "}. Hashes and timestamps are in ${" "}<${Anchor}
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
