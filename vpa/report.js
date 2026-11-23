import {
  Alert,
  Anchor,
  Badge,
  Box,
  Button,
  Container,
  Group,
  NativeSelect,
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

const github = "https://github.com/agentydragon/ducktape/";
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
  return row.vpas.length ? row.vpas[0].mode : "No direct VPA";
}

const mutatingModes = new Set([
  "Initial",
  "Recreate",
  "Auto",
  "InPlaceOrRecreate",
  "InPlace",
]);
const requestResources = ["cpu", "memory"];
const activeKinds = new Set(["Deployment", "StatefulSet", "DaemonSet"]);

function activeController(row) {
  return (
    row.scope === "controller-template" &&
    activeKinds.has(row.kind) &&
    row.desiredReplicas > 0
  );
}

function mutatingVpa(row) {
  return row.vpas.length === 1 && mutatingModes.has(row.vpas[0].mode);
}

function resourceHpa(row) {
  return row.hpas.some((hpa) =>
    hpa.metrics.some((metric) => requestResources.includes(metric.resource)),
  );
}

function sizedForBoth(row) {
  if (!mutatingVpa(row)) return false;
  return row.containers.some(({ name, template }) => {
    if (!template) return false;
    const policies = row.vpas[0].containers;
    const policy =
      policies.find((entry) => entry.containerName === name) ??
      policies.find((entry) => entry.containerName === "*");
    const resources = policy?.controlledResources ?? requestResources;
    return (
      policy?.mode !== "Off" &&
      requestResources.every((resource) => resources.includes(resource))
    );
  });
}

function fullyReadyDeployment(row) {
  return (
    row.kind === "Deployment" &&
    row.desiredReplicas > 0 &&
    row.readyReplicas === row.desiredReplicas
  );
}

const reviewGroups = [
  {
    label: "Add Initial coverage",
    matches: (row) =>
      fullyReadyDeployment(row) &&
      (row.vpas.length === 0 ||
        (row.vpas.length === 1 && mode(row) === "Off")) &&
      !resourceHpa(row),
  },
  {
    label: "Extend resource coverage",
    matches: (row) =>
      activeController(row) &&
      mutatingVpa(row) &&
      !sizedForBoth(row) &&
      !resourceHpa(row),
  },
  {
    label: "Promote to Recreate",
    matches: (row) =>
      fullyReadyDeployment(row) &&
      row.vpas.length === 1 &&
      mode(row) === "Initial" &&
      row.readyPodNodes.length >= 2,
  },
  {
    label: "Review HPA resources",
    matches: (row) => activeController(row) && resourceHpa(row),
  },
  {
    label: "Review other Pod owners",
    matches: (row) => row.scope === "pod-owner",
  },
];

function missingQuantity(value) {
  return value == null || Number.parseFloat(value) === 0;
}

function requestGap(row) {
  return row.containers
    .flatMap((container) => container.admitted)
    .filter((pod) =>
      requestResources.some((resource) =>
        missingQuantity(pod.requests[resource]),
      ),
    ).length;
}

function Inventory({ workloads }) {
  const [search, setSearch] = useState("");
  const [chosenMode, setChosenMode] = useState("All");
  const [missing, setMissing] = useState("All");
  const [reviewGroup, setReviewGroup] = useState("All");
  const [shown, setShown] = useState(30);
  const query = search.trim().toLowerCase();
  const groupFilter = reviewGroups.find((group) => group.label === reviewGroup);
  const modes = ["All", ...new Set(workloads.map(mode))];
  const filtered = workloads.filter((row) => {
    const text =
      `${row.namespace} ${row.name} ${row.kind} ${row.containers.map((c) => `${c.name} ${c.admitted.map((p) => p.pod).join(" ")}`).join(" ")}`.toLowerCase();
    const values = row.containers.flatMap((c) => c.admitted);
    const templates = row.containers.flatMap((c) =>
      c.template ? [c.template] : [],
    );
    const resourceMissing = (items, field) =>
      items.some(
        (p) =>
          missingQuantity(p[field].cpu) || missingQuantity(p[field].memory),
      );
    return (
      (!query || text.includes(query)) &&
      (!groupFilter || groupFilter.matches(row)) &&
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
    <${SimpleGrid} cols=${{ base: 1, sm: 2, lg: 4 }} spacing="md" mb="md">
      <${TextInput}
        label="Search namespace, workload, Pod or container"
        type="search"
        value=${search}
        onChange=${setFilter(setSearch)}
      />
      <${NativeSelect}
        label="Review cohort"
        value=${reviewGroup}
        onChange=${setFilter(setReviewGroup)}
        data=${["All", ...reviewGroups.map((group) => group.label)]}
      />
      <${NativeSelect}
        label="VPA mode"
        value=${chosenMode}
        onChange=${setFilter(setChosenMode)}
        data=${modes}
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
    <${Text} size="sm" c="dimmed" hiddenFrom="xl" my="xs"
      >Scroll the table sideways to see resource and usage details.<//
    >
    <${Table.ScrollContainer}
      minWidth=${1160}
      type="native"
      tabIndex=${0}
      aria-label="Workload VPA inventory"
    >
      <${Table} striped highlightOnHover withTableBorder verticalSpacing="sm">
        <${Table.Thead}
          ><${Table.Tr}
            ><${Table.Th}>Namespace / owner<//><${Table.Th}>Direct VPA policy<//
            ><${Table.Th}>Template and admitted resources<//
            ><${Table.Th}>Usage observations<//
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
                    >${`${row.kind} · ${row.scope === "pod-owner" ? "Pod owner; no template" : "controller template"} · ${row.activePods} active Pod(s)${row.desiredReplicas == null ? "" : ` · ${row.readyReplicas}/${row.desiredReplicas} Ready · ${row.readyPodNodes.length} Ready hosts`}`}<//
                  ><//
                >
                <${Table.Td}
                  >${mode(row) === "No direct VPA" ? html`<${Text} size="xs" c="dimmed">No direct VPA<//>` : html`<${Badge} variant="light" color=${mode(row) === "Off" ? "yellow" : "blue"}>${mode(row)}<//>`}${row.vpas.map((v) => html`<${Box} key=${v.name} mt="xs"><${Text} size="xs" fw=${600}>${v.name} · ${v.mode}<//>${v.containers.length ? v.containers.map((c) => html`<${Text} size="xs" key=${c.containerName}>${`${c.containerName ?? "*"}: ${c.mode === "Off" ? "off" : `${(c.controlledResources ?? ["cpu", "memory"]).join(", ")} · ${c.controlledValues ?? "RequestsAndLimits"}`}${c.minAllowed ? ` · min ${fmt(c.minAllowed)}` : ""}${c.maxAllowed ? ` · max ${fmt(c.maxAllowed)}` : ""}`}<//>`) : html`<${Text} size="xs">Default: CPU and memory requests and limits<//>`}<//>`)}<//
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
                  >${row.containers.map((c) => html`<${Box} key=${c.name} mb="xs"><${Text} size="xs" fw=${600}>${c.name}<//><${Text} size="xs">${`CPU max ${c.currentCpuMaxMillicores == null ? "unknown" : `${c.currentCpuMaxMillicores}m`} (${c.cpuSampledPods}/${c.admitted.length} Pods)`}<//><${Text} size="xs">${`memory max ${c.currentMemoryMaxMiB == null ? "unknown" : `${c.currentMemoryMaxMiB} MiB`} (${c.sampledPods}/${c.admitted.length} Pods)`}<//><${Text} size="xs">${`7-day max ${c.peak7dCurrentPodMiB == null ? "unknown" : `${c.peak7dCurrentPodMiB} MiB`} (${c.peakSampledPods}/${c.admitted.length} current Pods)`}<//><//>`)}<//
                >
                <${Table.Td}
                  >${row.vpas.flatMap((v) => v.recommendations.map((c) => html`<${Text} size="xs" key=${`${v.name}/${c.containerName}`}>${`${c.containerName}: ${c.target?.cpu ?? "unknown"} CPU / ${quantityMiB(c.target?.memory)}`}<//>`))}${row.hpas.map((h) => html`<${Text} size="xs" c="orange" key=${h.name}>HPA: ${h.name} (${h.metrics.map((metric) => (metric.resource ? [metric.resource, metric.container].filter(Boolean).join("/") : metric.type)).join(", ")})<//>`)}${row.vpas.length && !row.vpas.some((v) => v.recommendations.length) ? html`<${Text} size="xs">Recommendation unavailable<//>` : null}<//
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
  const [snapshot, setSnapshot] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const read = async (path) => {
      const response = await fetch(`${path}?v=202611230150`);
      if (!response.ok)
        throw new Error(`${path} returned HTTP ${response.status}`);
      return response.json();
    };
    Promise.all([read("./inventory.json"), read("./rollout-evidence.json")])
      .then(([inventory, review]) => setSnapshot({ inventory, review }))
      .catch((error) => setError(String(error)));
  }, []);
  useEffect(() => {
    if (!snapshot) return;
    function followHash() {
      const target = document.getElementById(window.location.hash.slice(1));
      if (!target) return;
      const details = target.closest("details");
      if (details) details.open = true;
      target.scrollIntoView();
    }
    followHash();
    window.addEventListener("hashchange", followHash);
    return () => window.removeEventListener("hashchange", followHash);
  }, [snapshot]);

  if (error)
    return html`<${Container} py="xl">
      <${Alert} color="red" title="Inventory could not be loaded">${error}<//>
    <//>`;
  if (!snapshot)
    return html`<${Container} py="xl"><${Text}>Loading VPA inventory…<//><//>`;

  const { inventory, review } = snapshot;
  const { counts, workloads } = inventory;
  const active = workloads.filter(activeController);
  const gapCount = workloads.reduce((total, row) => total + requestGap(row), 0);
  const chart = [...new Set(active.map(mode))]
    .map((label) => ({
      mode: label === "No direct VPA" ? "No VPA" : label,
      controllers: active.filter((row) => mode(row) === label).length,
    }))
    .sort((first, second) => second.controllers - first.controllers);
  return html`<${Container} component="main" size="xl" py="xl">
    <${Anchor} href="../">← All reports<//>
    <${Title} order=${1} mt="md">Cluster VPA rollout<//>
    <${Text} size="lg" mt="sm">
      ${`PostgreSQL accounts for 34 of the ${gapCount} running containers missing CPU or memory requests.`}
      ${" "}Start with CDviz. Verify that writes survive a switchover before
      sizing the other 15 replicated databases.
    <//>
    <${Text} size="sm" c="dimmed" mt="xs">
      ${`Inventory at ${inventory.collection_finished_utc.slice(11, 16)} UTC on 23 November 2026. ${counts.active_direct_vpa} of ${active.length} active controllers have a direct VPA.`}
    <//>
    <${Group} gap="md" my="md">
      <${Anchor} href="#remaining-work">Request gaps<//>
      <${Anchor} href="#promotion-waves">Eviction decisions<//>
      <${Anchor} href="#inventory">Search inventory<//>
      <${Anchor} href="#sources">Evidence<//>
    <//>
    <section id="remaining-work">
      <span id="batches"></span><span id="adoption-progress"></span>
      <${Title} order=${2} mt="xl">Close the ${gapCount} request gaps<//>
      <${Table}
        striped
        withTableBorder
        aria-label="Plan for the 45 missing container requests"
      >
        <${Table.Thead}
          ><${Table.Tr}
            ><${Table.Th} scope="col">Workload<//
            ><${Table.Th} scope="col">Gaps<//
            ><${Table.Th} scope="col">Next action<//><//
          ><//
        >
        <${Table.Tbody}
          >${review.remaining_wave_plan.waves.map(
            (w) =>
              html`<${Table.Tr} key=${w.label}
                ><${Table.Th} scope="row">${w.label}<//
                ><${Table.Td}>${w.containers}<//
                ><${Table.Td}
                  ><${Anchor} href=${"#" + w.anchor}>${w.action}<//><//
                ><//
              >`,
          )}<//
        >
      <//>
      <${Stack} id="open-rollout-decisions" gap="sm" mt="md">
        <${Title} order=${3} id="cnpg-next"
          >Pilot CDviz, then size one database at a time<//
        >
        <${Text}
          >Set the two PostgreSQL requests through the CNPG Cluster. Verify a
          usable backup, WAL catch-up and eligible node capacity. Send uniquely
          identified collector events across a switchover and check the
          committed rows for loss or duplication. Proceed when the client
          reconnects, the replica catches up and archiving resumes.<//
        >
        <${Text} id="cnpg-sequence"
          ><strong>After the pilot.</strong>${" "} Start the 15 replicated
          databases with telemetry and caches, then session and messaging
          services, then identity and state stores. Size each for its own
          startup and promoted-primary load. Schedule separate outages for Home
          Assistant and Session Backup. Establish a recovery source for Session
          Backup before its window.${" "}<${Anchor}
            href="./rollout-evidence.json"
            >Database order and per-service checks<//
          >.<//
        >
        <${Title} order=${3} id="operator-waves"
          >Prepare the operator changes in parallel<//
        >
        <${Text}
          ><strong>Alertmanager reloaders.</strong>${" "}Measure startup memory
          and reload CPU, then set both requests through
          alertmanagerSpec.containers. Roll one replica at a time and verify a
          config reload and a delivered notification.<//
        >
        <${Text}
          ><strong>NVIDIA device plugin.</strong>${" "}Use the Helm release.
          Compare its 15m CPU and 100 MiB target with startup demand. In a GPU
          window, verify the node still advertises the same device count and a
          disposable GPU job can use one.<//
        >
        <${Text} id="piraeus-next"
          ><strong>LINSTOR API.</strong>${" "} Schedule its singleton
          replacement separately from database switchovers. Proposed requests
          are 350m CPU and 1536 MiB. Recheck node headroom and agree an outage
          window. Monitor mounted-volume writes during turnover, then verify
          satellite reconnects, existing attachments and a new PVC through its
          first write.<//
        >
        <${Text} id="initial-admission"
          ><strong>Wait for seven ordinary replacements.</strong>${" "} Hubble
          UI, three Crossplane providers, node-exporter and Cilium Envoy passed
          admission dry runs. Check both requests on the replacement Pods. On
          wyrm2 also verify a fresh node scrape and a Gateway request.<//
        >
      <//>
    </section>
    <section id="promotion-waves">
      <${Title} order=${2} mt="xl">Which services can tolerate VPA eviction?<//>
      <${Stack} gap="sm" mt="md">
        <${Text}
          ><strong>CoreDNS and the VPA admission controller.</strong>${" "}
          Prepare two independent handoff probes. Continuously resolve a Service
          name for CoreDNS and submit owner-backed dry-run Pod creates for
          admission. Check each disruption budget, two Ready hosts and
          replacement capacity. Promote each service after its probe passes.<//
        >
        <${Text}
          ><strong>Cilium and KubeVirt.</strong>${" "}Resolve the Cilium
          operator’s Helm drift and verify new Gateway programming through
          leader handoff. For virt-api and virt-controller, verify client
          reconnect and VMI reconciliation.${" "}<${Anchor}
            href=${github + "pull/18520"}
            >Cilium rollout PR<//
          >.<//
        >
        <${Text}
          ><strong>Agentplane staging.</strong>${" "}All 13 service VPAs use
          Recreate. Electric became Ready 26 seconds after eviction. The
          remaining client check is a reconnect that delivers a new shape
          change. At the first Notifications eviction, follow a webhook through
          runner confirmation.<//
        >
        <${Text} id="worker-loss-placement"
          ><strong>Hold GitLab Rails.</strong>${" "} The placement model is 401
          MiB short for a surge at current targets. Verify headroom through a
          worker loss before promoting the draft. Keep HPA-owned CPU outside VPA
          control.${" "}<${Anchor} href=${github + "pull/18555"}
            >Rails sizing PR<//
          >.<//
        >
      <//>
    </section>
    <section id="current-state">
      <span id="coverage"></span>
      <${Title} order=${2} mt="xl">Inventory and evidence<//>
      <${Text} component="p"
        >For the next Initial policies, review Session Backup and GitLab toolbox
        for startup capacity and successful jobs. For crossplane-rbac-manager,
        review the 100m CPU limit before adding CPU to its memory-only policy.
        Keep singleton Ntfy in Initial and verify CDviz collector delivery
        before considering eviction. The inventory filters list the candidates
        and resource HPAs.<//
      >
      <details id="inventory">
        <summary>Search the workload snapshot and review cohorts</summary>
        <${Text} component="p" size="sm"
          >Active controllers by VPA mode at collection. Pods, hosts and dynamic
          owners use snapshot-local aliases.<//
        >
        <${BarChart}
          h=${150}
          data=${chart}
          dataKey="mode"
          series=${[{ name: "controllers", color: "blue.6" }]}
          tickLine="none"
          gridAxis="y"
        />
        <${Inventory} workloads=${workloads} />
      </details>
    </section>
    <section id="sources">
      <span id="rollout-status"></span><span id="piraeus-satellites"></span
      ><span id="piraeus-csi"></span><span id="piraeus-management"></span
      ><span id="kyverno-retirement"></span><span id="gitlab-registry"></span>
      <${Group} gap="sm" mt="md">
        <${Anchor} href="./inventory.json">Inventory snapshot<//>
        <${Anchor} href="./rollout-evidence.json"
          >Rollout measurements and wave plan<//
        >
        <${Anchor} href="./evidence.json">Earlier evidence<//>
        <${Anchor} href="./collect_inventory.py">Collector<//>
        <${Anchor} href="./manifest.json">Dates and hashes<//>
      <//>
    </section>
  <//>`;
}

mount(Report);
