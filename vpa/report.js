import {
  Alert,
  Anchor,
  Badge,
  Box,
  Code,
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
    detail:
      "Ready Deployments with no direct VPA or one Off VPA, excluding CPU or memory HPA targets.",
    action:
      "Batch compatible workloads under their owning chart or generator. For new policies, start in Initial with CPU and memory requests, retaining safe template requests and existing limits. Check state, startup peaks and operator ownership before selecting the wave.",
  },
  {
    label: "Extend resource coverage",
    matches: (row) =>
      activeController(row) &&
      mutatingVpa(row) &&
      !sizedForBoth(row) &&
      !resourceHpa(row),
    detail:
      "Active controllers in a mutating mode without one declared regular container configured for both resources, excluding resource HPAs.",
    action:
      "Review the existing per-container policy and its caps. Add the missing resource where compatible, preserve any existing requests-and-limits ownership, and keep deliberate container opt-outs until their owner clears them.",
  },
  {
    label: "Promote to Recreate",
    matches: (row) =>
      fullyReadyDeployment(row) &&
      row.vpas.length === 1 &&
      mode(row) === "Initial" &&
      row.readyPodNodes.length >= 2,
    detail:
      "Initial Deployments with every replica Ready and Ready Pods on at least two hosts.",
    action:
      "Group restart-safe services after checking PDBs, endpoint readiness, replacement capacity in each eligible topology and in-flight recovery. Two Ready hosts show placement at collection time. Verify the app works through a real replacement.",
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

function Stat({ value, label, detail }) {
  return html`<${Paper} withBorder p="md" radius="md">
    <${Text} size="xl" fw=${700}>${value}<//>
    <${Text} fw=${600}>${label}<//>
    <${Text} size="sm" c="dimmed">${detail}<//>
  <//>`;
}

function Inventory({ workloads, initialGroup }) {
  const [search, setSearch] = useState("");
  const [chosenMode, setChosenMode] = useState("All");
  const [missing, setMissing] = useState("All");
  const [reviewGroup, setReviewGroup] = useState(initialGroup);
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
  const [inventory, setInventory] = useState(null);
  const [error, setError] = useState("");
  const [selection, setSelection] = useState({ group: "All", revision: 0 });
  const openGroup = (group) =>
    setSelection((previous) => ({ group, revision: previous.revision + 1 }));

  useEffect(() => {
    fetch("./inventory.json?v=20261121065658")
      .then((response) => {
        if (!response.ok)
          throw new Error(`Inventory request returned HTTP ${response.status}`);
        return response.json();
      })
      .then(setInventory)
      .catch((error) => setError(String(error)));
  }, []);
  useEffect(() => {
    if (inventory && window.location.hash)
      document.getElementById(window.location.hash.slice(1))?.scrollIntoView();
  }, [inventory]);

  if (error)
    return html`<${Container} py="xl">
      <${Alert} color="red" title="Inventory could not be loaded">${error}<//>
    <//>`;
  if (!inventory)
    return html`<${Container} py="xl"><${Text}>Loading VPA inventory…<//><//>`;

  const { counts, workloads } = inventory;
  const active = workloads.filter(activeController);
  const mutating = active.filter(mutatingVpa);
  const both = active.filter(sizedForBoth);
  const noVpa = counts.active_controllers - counts.active_direct_vpa;
  const gapCount = workloads.reduce((total, row) => total + requestGap(row), 0);
  const chart = [...new Set(active.map(mode))]
    .map((label) => ({
      mode: label === "No direct VPA" ? "No VPA" : label,
      controllers: active.filter((row) => mode(row) === label).length,
    }))
    .sort((first, second) => second.controllers - first.controllers);
  const storageGaps = [
    ["Piraeus", (row) => row.namespace === "piraeus-datastore"],
    ["SeaweedFS CSI", (row) => row.namespace === "seaweedfs-csi-system"],
    [
      "CNPG",
      (row) => row.apiGroup === "postgresql.cnpg.io" && row.kind === "Cluster",
    ],
  ].map(([name, matches]) => ({
    name,
    containers: workloads
      .filter(matches)
      .reduce((total, row) => total + requestGap(row), 0),
  }));
  const registry = workloads.find(
    (row) =>
      row.namespace === "gitlab-spike" &&
      row.name === "gitlab-spike-registry" &&
      row.kind === "Deployment",
  );
  const registryContainer = registry?.containers.find(
    (container) => container.name === "registry",
  );
  const registryRequests = [
    ...new Set(
      registryContainer?.admitted.map(
        ({ requests }) =>
          `${requests.cpu ?? "unset"} CPU and ${requests.memory ?? "unset"} memory`,
      ),
    ),
  ];
  const ntfy = workloads.find(
    (row) =>
      row.namespace === "ntfy" &&
      row.name === "ntfy" &&
      row.kind === "Deployment",
  );
  const revision = inventory.flux_source_revision.split(":").at(-1);
  const link = (label) =>
    html`<${Anchor} href="#inventory" onClick=${() => openGroup(label)}
      >${label}<//
    >`;

  return html`<${Container} component="main" size="xl" py="xl">
    <${Anchor} href="../">← All reports<//>
    <${Title} order=${1} mt="md">Cluster VPA rollout<//>
    <${Text} size="lg" mt="sm"
      >Expand CPU and memory request sizing in Initial mode. Use Recreate where
      the workload can recover safely through Pod replacement.<//
    >
    <${Text} size="sm" c="dimmed" mt="xs">
      ${`Kubernetes snapshot ${inventory.collection_started_utc} to ${inventory.collection_finished_utc}. Refresh the inventory before dispatching a wave.`}
    <//>
    <${Group} gap="md" my="md">
      <${Anchor} href="#current-state">Coverage<//>
      <${Anchor} href="#rollout-status">Rollout checks<//>
      <${Anchor} href="#remaining-work">Next waves<//>
      <${Anchor} href="#inventory">Search inventory<//>
      <${Anchor} href="#sources">Evidence<//>
    <//>

    <section id="current-state">
      <span id="coverage"></span>
      <${Title} order=${2} mt="xl">Coverage at the snapshot<//>
      <${SimpleGrid} cols=${{ base: 1, sm: 3 }} spacing="md" my="md">
        <${Stat}
          value=${`${counts.active_direct_vpa} / ${active.length}`}
          label="Active controllers with a direct VPA"
          detail=${`${noVpa} have no direct VPA. Off policies count as coverage here.`}
        />
        <${Stat}
          value=${`${mutating.length} / ${active.length}`}
          label="Controllers enabled for sizing"
          detail="One direct VPA in a mode that can update Pods at admission or later."
        />
        <${Stat}
          value=${`${both.length} / ${active.length}`}
          label="Controllers set to size CPU and memory"
          detail="At least one declared regular container can receive both requests."
        />
      <//>
      <${SimpleGrid} cols=${{ base: 1, md: 2 }} spacing="md">
        <${Paper} withBorder p="md" radius="md">
          <${Title} order=${3} size="h4">Active controllers by VPA mode<//>
          <${BarChart}
            h=${210}
            data=${chart}
            dataKey="mode"
            series=${[{ name: "controllers", color: "blue.6" }]}
            tickLine="none"
            gridAxis="y"
          />
          <${Text} size="sm" c="dimmed">
            ${chart.map(({ mode, controllers }) => `${mode} ${controllers}`).join(" · ")}
          <//>
          <${Text} size="sm" c="dimmed" mt="xs">
            ${`Active means desired replicas or eligible DaemonSet nodes above zero. The cluster has ${counts.all_vpa_objects} VPA objects across all targets.`}
          <//>
        <//>
        <${Paper} withBorder p="md" radius="md">
          <${Title} order=${3} size="h4">Gaps in admitted Pod requests<//>
          <${Text} component="p">
            ${`${gapCount} of ${counts.regular_containers} regular containers across ${counts.active_pods} active Pods lacked at least one request. ${counts.missing_requests_cpu} lacked CPU and ${counts.missing_requests_memory} lacked memory.`}
          <//>
          <${Text} component="p">
            ${`Storage and database gaps include ${storageGaps.map(({ name, containers }) => `${name} (${containers})`).join(", ")}. Work with the operator or storage owner on sizing and recovery.`}
          <//>
          <${Text} component="p" mb=${0}>
            Initial sizes new Pods at admission. Existing Pods keep their
            admitted requests until replaced. Recreate lets the VPA evict
            running Pods. Compare the policy, template and admitted Pod
            resources in the inventory.
          <//>
        <//>
      <//>
    </section>

    <section id="rollout-status">
      <${Title} order=${2} mt="xl">Verify the latest rollouts<//>
      <${Stack} gap="sm" mt="sm">
        <${Text} id="application-wave">
          <strong>Eight application policies.</strong> At 06:58:34 UTC,
          Electric, Study Casino and the GitLab exporter had the new Initial CPU
          and memory policy. They still awaited recommendations on those
          generations. Five Goldilocks VPAs still had older policies even though
          the Deployment annotations had changed: CDViz collector, Home
          Assistant, Plaid pgweb, Plaid Spend and the GitLab runner manager. By
          07:00:24 UTC Home Assistant had also converged, and all four updated
          VPAs had current CPU targets. Scoped Goldilocks reconciliation at
          07:11 UTC brought CDViz, Plaid pgweb, Plaid Spend and the GitLab
          runner manager onto the new policy. The audit found unchanged
          unrelated VPA specs, Pod UIDs and Ready endpoints. At 07:12:53 UTC all
          eight policies matched, with four recommendation updates still
          pending. Check generation and owner-matched admission after the
          expected 07:15 cycle. ${" "}<${Anchor} href=${github + "pull/18519"}
            >Application batch<//
          >.
        <//>
        <${Text} id="staging-frontend">
          <strong>Agentplane staging frontend.</strong> Both VPA-initiated
          replacements became Ready in sequence by 06:46:31 UTC. Two Ready
          endpoints on separate OVH workers had 15m CPU and 100 MiB memory
          requests. RequestsOnly preserved the 128 MiB limit. The PDB again
          allowed one disruption. ${" "}<${Anchor} href=${github + "pull/18523"}
            >Frontend rollout<//
          >.
        <//>
        <${Text} id="snapshot-controller">
          <strong>Snapshot controller.</strong> At 06:59 UTC, two replicas were
          Ready on separate OVH workers, the leader lease renewed and the PDB
          allowed one disruption. Its Recreate policy retained
          RequestsAndLimits. Generation 2 was pending at 06:59 UTC and had
          current targets of 15m CPU and 100 MiB memory after the 07:00 cycle.
          The isolated handoff completed an in-flight snapshot. Confirm ordinary
          controller recovery and scheduled snapshot completion before relying
          on that rehearsal. ${" "}<${Anchor} href=${github + "pull/18499"}
            >Controller policy and rehearsal<//
          >.
        <//>
        <${Text} id="admission-availability">
          <strong>VPA admission availability.</strong> Three Ready webhook
          endpoints were on distinct hosts at 06:57 UTC. The PDB required two
          and allowed one disruption at 06:59 UTC. The webhook's Ignore policy
          and five-second timeout still allow template admission when the
          webhook is unavailable. ${" "}<${Anchor} href=${github + "pull/18531"}
            >Webhook rollout<//
          >. ${" "}Seven Flux controllers already passed Initial admission dry
          runs. Kustomize and source requested 977m and 920m CPU at the 06:00
          check. ${" "}<${Anchor} href=${github + "pull/18494"}
            >Flux sizing and recovery<//
          >.
        <//>
        <${Text} id="virtualization-wave">
          <strong>KubeVirt and CDI.</strong> At 06:47:18 UTC, all six Initial
          VPAs had current recommendations. All six owner-matched dry runs
          applied their CPU and memory requests with unchanged limits and all
          nine incumbent Pods stayed Ready. Virt-controller admitted about 283
          MiB, below its sampled 332 MiB startup working set. Check actual
          reservations and startup on natural replacements. ${" "}<${Anchor}
            href=${github + "pull/18518"}
            >Controller sizing<//
          >.
        <//>
      <//>
    </section>

    <section id="remaining-work">
      <span id="batches"></span><span id="adoption-progress"></span>
      <${Title} order=${2} mt="xl">Next waves<//>
      <${Text} component="p">
        Counts are review queues from this snapshot. A workload can appear in
        both the resource and Recreate queues. Confirm ownership and restart
        behavior before choosing the cohort. Ownership and rollout gates are
        tracked in the ${" "}<${Anchor}
          href=${github + "blob/devel/cluster/plans/vpa.md"}
          >cluster rollout plan<//
        >.
      <//>
      <${SimpleGrid} cols=${{ base: 1, md: 3 }} spacing="md">
        ${reviewGroups
          .filter((group) => group.action)
          .map(
            (group) =>
              html` <${Paper} withBorder p="md" radius="md" key=${group.label}>
                <${Title} order=${3} size="h4">${group.label}<//>
                <${Text} fw=${600} mt="xs"
                  >${workloads.filter(group.matches).length} workloads<//
                >
                <${Text} size="sm" c="dimmed" mt="xs">${group.detail}<//>
                <${Text} component="p">${group.action}<//>
                <${Anchor}
                  href="#inventory"
                  aria-label=${`Open ${group.label} review cohort`}
                  onClick=${() => openGroup(group.label)}
                  >Open cohort<//
                >
              <//>`,
          )}
      <//>
      <${Title} order=${3} mt="lg">Open rollout decisions<//>
      <${Stack} id="open-rollout-decisions" gap="sm" mt="sm">
        <${Text}>
          <strong>Ready for review at 07:00 UTC.</strong> Four static
          application Deployments can add Initial sizing with explicit owners.
          Central Sampler starts in Off; its Initial follow-up needs actual CPU
          recommendations and OVH placement. GitLab Shell separates Helm CPU
          requests from its memory VPA while preserving the HPA metric.
          ${" "}<${Anchor} href=${github + "pull/18543"}>Static applications<//>
          ${" · "}<${Anchor} href=${github + "pull/18553"}>Sampler gate<//>
          ${" · "}<${Anchor} href=${github + "pull/18549"}>GitLab Shell<//>.
        <//>
        <${Text}>
          <strong>Evidence still needed.</strong> Rails needs headroom for a
          concurrent 3 GiB Sidekiq surge. CDI operator needs stable leader
          renewal and virt-handler needs per-node replacement evidence under VM
          demand. Tofu needs a representative apply on its current image. Loki
          read needs tail reconnect and memory-pressure evidence.
          ${" "}<${Anchor} href=${github + "pull/18555"}>Rails<//>
          ${" · "}<${Anchor} href=${github + "pull/18528"}
            >CDI operator and virt-handler<//
          >
          ${" · "}<${Anchor} href=${github + "pull/18498"}>Tofu<//>
          ${" · "}<${Anchor} href=${github + "pull/18542"}>Loki read<//>.
        <//>
        <${Text} id="worker-loss-placement">
          <strong>Schedule overlapping surges with a placement check.</strong>
          ${" "}The KubeVirt/CDI model left 190m CPU in its tightest feasible
          single-OVH-worker-loss case. Two cases failed when selected earlier
          CPU peaks coincided, with an 88m deficit in the tightest case. The
          later static-application model reached 62 MiB of memory headroom in
          one placement. These estimates hold unrelated reservations fixed.
          Recheck with actual new Pod requests and active rollouts.
          ${" "}<${Anchor} href=${github + "pull/18518"}
            >Controller placement<//
          >
          ${" · "}<${Anchor} href=${github + "pull/18543"}
            >Application placement<//
          >.
        <//>
      <//>
      <${Title} order=${3} mt="lg">Apply these gates across the wave<//>
      <${Stack} gap="sm" mt="sm">
        <${Text}>
          <strong>Resource ownership.</strong> For targets with a CPU or memory
          HPA, keep that resource out of VPA control.${" "}
          ${`The snapshot has ${workloads.filter(reviewGroups[3].matches).length} active resource HPA targets. `}
          ${link("Review HPA resources")}. External metric scaling calls for its
          own startup and capacity check. Existing RequestsAndLimits policies
          keep that ownership.
        <//>
        <${Text}>
          <strong>Admission and capacity.</strong> Verify recommendations and
          bounds with owner-backed admission checks, then inspect newly admitted
          Pods. Budget concurrent rollout surge against eligible nodes. Template
          requests cover admission without a recommendation.
        <//>
        <${Text}>
          <strong>Custom and dynamic ownership.</strong>${" "}
          ${`The snapshot has ${counts.pod_owner_rows} other active Pod owner rows, including CNPG clusters, virtual machines and dynamic Jobs. `}
          ${link("Review other Pod owners")} for ownership. Verify the supported
          control path before adding policy. Keep Agentplane dynamic Sandboxes
          disabled for namespace-wide Goldilocks and manage static services
          explicitly.
        <//>
      <//>
      <${SimpleGrid} cols=${{ base: 1, md: 2 }} spacing="md" mt="lg">
        <${Paper} id="gitlab-registry" withBorder p="md" radius="md">
          <${Title} order=${3} size="h4"
            >GitLab registry uses a resource split<//
          >
          <${Text} component="p">
            ${
              registry
                ? `The registry was ${registry.readyReplicas}/${registry.desiredReplicas} Ready on ${registry.readyPodNodes.length} hosts in ${mode(registry)}. The admitted registry requests were ${registryRequests.join(" or ") || "unavailable"}.`
                : "The current inventory has no registry Deployment row."
            }
            ${" "}Registry uses CPU utilization. Shell, Sidekiq and Webservice
            use absolute CPU quantities. Keep CPU out of their VPA policies.
            Verify actual replacement capacity for memory changes.
          <//>
          <${Group} gap="sm">
            <${Anchor} href=${github + "pull/18477"}>Registry S3 ACL change<//>
            <${Anchor} href=${github + "pull/18465"}>Registry VPA policy<//>
            <${Anchor}
              href=${github + "blob/a55b569085ed24128d1606c1febf8bec89181598/vpa/report.js"}
              >04:40 UTC report<//
            >
          <//>
        <//>
        <${Paper} withBorder p="md" radius="md">
          <${Title} order=${3} size="h4">Ntfy stays in Initial<//>
          <${Text} component="p" mb=${0}>
            ${
              ntfy
                ? `Ntfy was ${ntfy.readyReplicas}/${ntfy.desiredReplicas} Ready in ${mode(ntfy)} at collection time.`
                : "The current inventory has no ntfy Deployment row."
            }
            ${" "}Size new Pods without VPA eviction while the service runs one
            replica. CDviz's collector and the staging Electric singleton also
            need non-evicting sizing. Check their work and recovery paths before
            proposing a restart mode.
          <//>
        <//>
      <//>
    </section>

    <section id="inventory">
      <${Title} order=${2} mt="xl">Search the workload snapshot<//>
      <${Text} component="p">
        Search an owner, namespace, Pod or container. A row is a Deployment,
        StatefulSet, DaemonSet or CronJob template, or an active Pod owner whose
        template is outside that set. Template resources come from the stored
        controller. Admitted resources come from non-terminating Running or
        Pending Pods.
      <//>
      <${Inventory}
        key=${selection.revision}
        workloads=${workloads}
        initialGroup=${selection.group}
      />
    </section>

    <section id="sources">
      <span id="kyverno-retirement"></span>
      <${Title} order=${2} mt="xl">Evidence and method<//>
      <${Stack} gap="sm" mt="sm">
        <${Text}>
          The collector makes sequential API reads. Pod attribution follows
          controller references by UID through ReplicaSets or Jobs where
          available. Direct VPA targets match on API group, kind, namespace and
          name. The CPU-plus-memory count resolves named container policy before
          wildcard and VPA defaults. Recommendations, admitted resource changes
          and replacement behavior each require separate verification.
        <//>
        <${Text}>
          CPU and current memory use metrics-server samples. Seven-day
          working-set maxima use Mimir and match current Pod names. The query
          window ends at ${inventory.history_end_utc}. Replaced Pods can be
          absent and reused names can include prior Pods. Unknown means there
          was no sample. Init and ephemeral containers are outside the resource
          counts.
        <//>
        <${Text}>
          ${`Flux source was observed at ${inventory.flux_source_observed_utc} as `}
          <${Anchor} href=${github + "tree/" + revision}
            ><${Code}>${revision.slice(0, 10)}<//><//
          >. The Git revision identifies the source artifact. The inventory
          records the workloads present during the collection window.
        <//>
        <${Text}>
          The earlier rollout evidence retains its original timestamps. The
          preceding report covers the seven Flux controllers, Loki and Kyverno
          placement, staging, Platform/MCP and NFD. ${" "}<${Anchor}
            href=${github + "blob/10bbb9e753/vpa/report.js"}
            >Previous rollout evidence<//
          >.
        <//>
        <${Group} gap="sm">
          <${Anchor} href="./inventory.json">Snapshot inventory<//>
          <${Anchor} href="./collect_inventory.py">Collector<//>
          <${Anchor} href="./evidence.json">Earlier evidence<//>
          <${Anchor} href="./manifest.json">Dates and hashes<//>
        <//>
      <//>
    </section>
  <//>`;
}

mount(Report);
