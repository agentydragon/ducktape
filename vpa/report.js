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
  const [snapshot, setSnapshot] = useState(null);
  const [error, setError] = useState("");
  const [selection, setSelection] = useState({ group: "All", revision: 0 });
  const openGroup = (group) =>
    setSelection((previous) => ({ group, revision: previous.revision + 1 }));

  useEffect(() => {
    const read = async (path) => {
      const response = await fetch(`${path}?v=202611230116`);
      if (!response.ok)
        throw new Error(`${path} returned HTTP ${response.status}`);
      return response.json();
    };
    Promise.all([read("./inventory.json"), read("./rollout-evidence.json")])
      .then(([inventory, review]) => setSnapshot({ inventory, review }))
      .catch((error) => setError(String(error)));
  }, []);
  useEffect(() => {
    if (snapshot && window.location.hash)
      document.getElementById(window.location.hash.slice(1))?.scrollIntoView();
  }, [snapshot]);

  if (error)
    return html`<${Container} py="xl">
      <${Alert} color="red" title="Inventory could not be loaded">${error}<//>
    <//>`;
  if (!snapshot)
    return html`<${Container} py="xl"><${Text}>Loading VPA inventory…<//><//>`;

  const { inventory, review } = snapshot;
  const { counts, workloads } = inventory;
  const nodeRollout = review.piraeus_csi_node_rollout;
  const fleet = review.piraeus_ha_and_satellite_rollout.satellites;
  const io = nodeRollout.wyrm2.continuous_probe;
  const fresh = nodeRollout.wyrm2.fresh_probe;
  const gitlab = review.gitlab_capacity;
  const railsSurge = gitlab.scenarios.find(
    (scenario) => scenario.name === "Rails surge",
  );
  const vmi = review.kubevirt.post_handoff_vmi_samples[0];
  const clock = (stamp) => stamp.slice(11, 19);
  const remaining = (mib) =>
    mib < 0
      ? `${(-mib).toLocaleString("en-US")} MiB short`
      : `${mib.toLocaleString("en-US")} MiB free`;
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
      >All nine Piraeus satellites are sized. Only the central LINSTOR API still
      lacks requests in Piraeus. Across the cluster, PostgreSQL accounts for 34
      of the 45 remaining regular containers with a request gap.<//
    >
    <${Text} size="sm" c="dimmed" mt="xs">
      ${`Kubernetes snapshot ${inventory.collection_started_utc} to ${inventory.collection_finished_utc}.`}
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
            Five of these gaps belong to Hubble UI and Crossplane providers.
            Their Initial VPAs supplied both requests in the ${" "}<${Anchor}
              href="#initial-admission"
              >admission dry runs<//
            >. The five running containers still count toward the 45 gaps.
          <//>
        <//>
      <//>
    </section>

    <section id="rollout-status">
      <${Title} order=${2} mt="xl">Recent rollouts<//>
      <${Text} mt="sm" id="piraeus-satellites">
        <strong>All nine satellites and nine HA controllers recovered.</strong>
        ${" "}The HA agents each reserved 50m CPU and 128 MiB and completed at
        least two reconciliation loops. On wyrm2, an existing mount passed 566
        consecutive fsync/readback checks through satellite replacement. A fresh
        local SSD claim verified its first 1 MiB in 9.2 seconds. During the
        other eight replacements, a two-replica OVH volume passed 574
        consecutive checks with a longest interval of 1.172 seconds. The probe
        covered the mounting node and both diskful nodes. All
        ${fleet.original_attached_volumes} original attachments and
        ${fleet.original_resource_assignments} resource assignments survived.
        All nine satellites were online with zero container restarts at 00:58
        UTC on 23 November. Restoring RollingUpdate preserved all nine Pod UIDs.
        The probes exercised mounted I/O and fresh provisioning; storage-fault
        failover timing remains unmeasured. ${" "}<${Anchor}
          href=${github + "pull/19220"}
          >HA agents<//
        >
        ${" · "}<${Anchor} href=${github + "pull/19229"}>Canary<//>
        ${" · "}<${Anchor} href=${github + "pull/19232"}>Fleet sizing<//>
        ${" · "}<${Anchor} href=${github + "pull/19235"}>Restored policy<//>.
      <//>
      <${Text} mt="sm" id="piraeus-csi">
        <strong>All nine CSI node plugins registered with kubelet.</strong>
        ${" "}Each node now reserves 105m CPU and 192 MiB for the driver,
        registrar and liveness sidecar. All 27 containers were Ready with zero
        restarts. On wyrm2, the original consumer completed
        ${" "}${io.consecutive_verified_64_kib_blocks} consecutive 64 KiB
        writes, fsyncs and verified reads across plugin replacement.
        ${` The longest interval between successful checks was ${io.max_interval_seconds.toFixed(2)} seconds over the full probe and ${io.max_interval_during_handoff_seconds.toFixed(2)} seconds during the observed handoff. `}
        After kubelet acknowledged the new driver, a second consumer mounted a
        fresh local SSD claim and verified four 1 MiB blocks.
        ${` Its first verified read came ${fresh.seconds_to_first_io.toFixed(1)} seconds after Pod creation. `}
        Both disposable volumes were reclaimed. The application probes covered
        one node. ${" "}<${Anchor} href=${github + "pull/19212"}
          >Node plugin requests and sizing<//
        >.
      <//>
      <${Text} mt="sm">
        <strong
          >The six central CSI controller containers recovered their four
          leases.</strong
        >
        ${" "}They were Ready with zero restarts. A disposable local SSD on
        wyrm2 bound in two seconds, attached in three and passed its first fsync
        and SHA-256 readback in 17.3 seconds from eligible consumer creation.
        This test ran after the central controller handoff. The initial consumer
        selected OVH workers with no local SSD capacity. ${" "}<${Anchor}
          href=${github + "pull/19205"}
          >Central controller requests<//
        >.
      <//>
      <${Text} mt="sm" id="piraeus-management">
        <strong
          >Both Piraeus management Pods received static requests on 22
          November.</strong
        >
        ${" "}The operator manager was Ready at 200m CPU and 192 MiB, and the
        affinity controller at 15m CPU and 64 MiB. Both acquired their leader
        leases with zero container restarts. The webhook retained a Ready
        endpoint throughout the replacement. The affinity lease changed hands 54
        seconds after the old leader’s last renewal. Before and after the
        rollout, all nine satellites were connected and LINSTOR reported 194
        UpToDate and 52 Diskless volume instances. Application I/O was
        unmeasured. ${" "}<${Anchor} href=${github + "pull/19099"}
          >Management requests PR<//
        >
        ${" · "}<${Anchor} href="./rollout-evidence.json">Rollout evidence<//>.
      <//>
      <${Text} mt="sm">
        <strong>KubeVirt recovered VMI reconciliation.</strong> The controller
        lease moved at ${clock(review.kubevirt.controller_lease_acquired_utc)}
        UTC. A new BuildBuddy VMI created at ${clock(vmi.created_utc)} UTC
        reached Ready at ${clock(vmi.ready_utc)} UTC. The API and controller
        were each 2/2 Ready across two hosts. The API had
        ${review.kubevirt.api_ready_endpoints} Ready service backends, and each
        PDB allowed one disruption. API client recovery during turnover remains
        open. ${" "}<${Anchor} href=${github + "pull/19086"}>Placement PR<//>.
      <//>
      <${Text} mt="sm">
        <strong>Notifications moved to Recreate.</strong> Both replicas were
        Ready on the image containing the delivery-backoff fix. The rollout
        traced a webhook through the replacement to runner confirmation. The
        live VPA is Recreate and its PDB allows one disruption. The first
        updater eviction is still unobserved. ${" "}<${Anchor}
          href=${github + "pull/19101"}
          >Rollout evidence<//
        >.
      <//>
      <${Text} mt="sm">
        <strong>SeaweedFS CSI has requests across the Ready fleet.</strong> The
        controller was 1/1 Ready and the node and mount DaemonSets were both
        6/6. Every sampled regular container had CPU and memory requests.
        ${" "}<${Anchor} href=${github + "pull/19080"}>CSI PR<//>.
      <//>
      <${Text} mt="sm">
        <strong>CNPG sizing needs a controlled rollout.</strong> All four
        Clusters recovered after the request wave, but three write endpoints
        were unready for roughly three minutes each during primary restarts. At
        11:24 UTC on 22 November, a Study Casino switchover test had an
        11.1-second gap between successful client reads and ten failed attempts
        before reconnection; it did not test committed writes. Both instances
        and archiving recovered. This is one observation, not an outage bound.
        ${" "}<${Anchor} href=${github + "pull/19102"}>Database sizing PR<//
        >${" · "}<${Anchor} href=${github + "pull/19119"}>Switchover test<//
        >${" · "}<${Anchor} href="./rollout-evidence.json">Dated evidence<//>.
      <//>
    </section>

    <section id="remaining-work">
      <span id="batches"></span><span id="adoption-progress"></span>
      <${Title} order=${2} mt="xl">Next waves<//>
      <${Stack} id="open-rollout-decisions" gap="sm" mt="sm">
        <${Text} id="piraeus-next">
          <strong
            >1. Finish Piraeus with a scheduled LINSTOR API replacement.</strong
          >
          ${" "}At 01:00 UTC on 23 November, 91 of 92 Piraeus containers had
          both requests. Propose 350m CPU and 1536Mi memory through the
          controller Pod template. Keep its VPA in Off. The seven-day peaks were
          276m CPU and 1122 MiB working set. The current Pod’s first 30 minutes
          peaked at 300m and 1064 MiB. CPU uses five-minute averages. The VPA
          target was 163m and 1251 MiB. All six selector-matching nodes had room
          for the proposed requests at the capacity check.
        <//>
        <${Text}>
          The Deployment runs one replica with Recreate. New provisioning and
          attachment operations can wait or retry while its API is unavailable.
          The current Pod recorded six restarts and reached its current Ready
          condition 17m 31s after creation. Review that startup history and
          confirm a tolerable API outage before merging. Recheck node headroom,
          record attachments and replica health, and start an authenticated API
          read loop plus a mounted fsync/readback probe. Time the API gap and a
          disposable PVC requested during turnover through binding, attachment
          and first verified write. Stop on I/O failure or replica degradation.
          If recovery stalls, inspect startup and revert the request change if
          it blocks scheduling. Close after all nine satellites reconnect,
          original attachments persist and the test volume is reclaimed.
          ${" "}<${Anchor} href="./rollout-evidence.json">Sizing measurements<//
          >.
        <//>
        <${Text} id="cnpg-next">
          <strong
            >2. Pilot one replicated CNPG database before sizing the
            cohort.</strong
          >
          ${" "}PostgreSQL accounts for 34 gaps across 16 two-instance clusters
          and two singletons. Start with cdviz/db. Both instances were Ready and
          archiving was healthy. Size its two postgres containers through
          Cluster.spec.resources. It has no direct VPA. Keep any future database
          VPA recommendation-only. Confirm a usable backup, streaming replica
          caught up to a recorded WAL position and placement headroom. Use
          CNPG’s switchover update method. Track unique collector events through
          reconnect and query their committed rows for loss or duplication. The
          previous Study Casino test measured an 11.1-second read gap and did
          not exercise writes. Advance one replicated database at a time after
          that check, replication and archiving pass. Home Assistant and Session
          Backup each need a singleton maintenance window. ${" "}<${Anchor}
            href=${github + "blob/devel/cluster/docs/cnpg_conventions.md#r7-cnpg-vpas-are-recommendation-only"}
            >CNPG rollout procedure<//
          >.
        <//>
        <section id="initial-admission">
          <${Title} order=${3} size="h4"
            >Five containers passed Initial admission<//
          >
          <${Text} component="p">
            At 01:16 UTC on 23 November, server-side dry runs used the four
            active ReplicaSets as Pod owners. In every response the VPA set both
            requests to its live recommendation. The four original Pods remained
            Ready.
          <//>
          <${Table}
            striped
            withTableBorder
            aria-label="Requests returned by VPA admission"
          >
            <${Table.Thead}
              ><${Table.Tr}>
                <${Table.Th} scope="col">Container or provider<//>
                <${Table.Th} scope="col">CPU request<//
                ><${Table.Th} scope="col">Memory request<//>
              <//><//
            >
            <${Table.Tbody}>
              ${review.initial_admission_check.results.map(
                ({ label, dry_run_requests }) =>
                  html`<${Table.Tr} key=${label}
                    ><${Table.Th} scope="row">${label}<//>
                    <${Table.Td}>${dry_run_requests.cpu}<//
                    ><${Table.Td}>${quantityMiB(dry_run_requests.memory)}<//><//
                  >`,
              )}
            <//>
          <//>
          <${Text} component="p" mt="sm"
            >Keep the four Initial policies. On each workload’s next ordinary
            replacement, compare the running Pod’s requests with the
            recommendation at admission. Count the gap as closed once the new
            Pod has both requests. Memory in the table is rounded to the nearest
            MiB. ${" "}<${Anchor} href="./rollout-evidence.json"
              >Exact quantities and admission evidence<//
            >.
          <//>
        </section>
        <${Text}>
          <strong
            >Observe Notifications during its first confirmed updater
            eviction.</strong
          >
          ${" "}Verify a serving endpoint, replacement capacity, SQL readiness
          and delivery of a webhook through runner confirmation.
        <//>
        <${Text} id="worker-loss-placement">
          <strong>Hold Rails admission sizing in draft.</strong> The 22 November
          model is ${remaining(railsSurge.target.min_remaining_mib)} for a Rails
          surge and either Rails-host failure at current targets. The loss case
          reaches ${remaining(railsSurge.sensitivity.min_remaining_mib)} at the
          Rails upper estimate and Sidekiq ceiling. A healthy Sidekiq-only surge
          fits. ${" "}<${Anchor} href=${github + "pull/18555"}>Rails draft<//>.
        <//>
        <${Table.ScrollContainer} minWidth=${515}>
          <${Table}
            striped
            withTableBorder
            aria-label="GitLab workload placement model"
          >
            <${Table.Thead}
              ><${Table.Tr}>
                <${Table.Th} scope="col">Scenario<//>
                <${Table.Th} scope="col">Current targets<//>
                <${Table.Th} scope="col">Upper estimate and ceiling<//>
              <//><//
            >
            <${Table.Tbody}>
              ${gitlab.scenarios.map(
                (scenario, index) =>
                  html`<${Table.Tr} key=${index}>
                    <${Table.Th} scope="row">${scenario.name}<//>
                    <${Table.Td}
                      >${remaining(scenario.target.min_remaining_mib)}<//
                    >
                    <${Table.Td}
                      >${remaining(scenario.sensitivity.min_remaining_mib)}<//
                    >
                  <//>`,
              )}
            <//>
          <//>
        <//>
        <${Text} size="sm" c="dimmed">
          Values show the tightest surviving worker in the best modeled
          placement across three eligible workers. Reserving 10 GiB and 2,500m
          per worker before Rails, Sidekiq and Central Sampler fits the modeled
          upper case. Budget additional capacity for unrelated Pods displaced by
          a lost worker. ${" "}<${Anchor} href="./rollout-evidence.json"
            >Inputs and placements<//
          >.
        <//>
        <${Text}>
          <strong>Prove KubeVirt client recovery before mode promotion.</strong>
          Exercise an ordinary API client through turnover, verify placement
          headroom and PDBs, and evaluate Recreate per controller. The VMI
          result clears the new-instance check for the observed handoff.
        <//>
      <//>
      <${Title} order=${3} mt="lg">Review cohorts<//>
      <${Text} component="p">
        Counts are review queues from this snapshot. A workload can appear in
        both the resource and Recreate queues. Confirm ownership and restart
        behavior before choosing the cohort. Ownership and rollout gates are
        tracked in the ${" "}<${Anchor}
          href=${github + "blob/" + revision + "/cluster/plans/vpa.md"}
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
      <${Title} order=${3} mt="lg">Apply these gates across the wave<//>
      <${Stack} gap="sm" mt="sm">
        <${Text}>
          <strong>Resource ownership.</strong> CNPG VPAs stay in Off mode. Set
          requests through the Cluster and Barman ObjectStore specs. For targets
          with a CPU or memory HPA, keep that resource out of VPA control.${" "}
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
        StatefulSet, DaemonSet or CronJob template, or another active Pod owner.
        Pods, hosts and dynamic owners use aliases local to this snapshot.
        Admitted resources come from non-terminating Running or Pending Pods.
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
          previous report includes Claude Session Sync admission, GitLab Shell
          resource ownership, and earlier capacity evidence. ${" "}<${Anchor}
            href=${github + "blob/f3d018964300363fedd2ab782632a99d42e198f0/vpa/report.js"}
            >Previous rollout evidence<//
          >.
        <//>
        <${Group} gap="sm">
          <${Anchor} href="./inventory.json">Snapshot inventory<//>
          <${Anchor} href="./collect_inventory.py">Collector<//>
          <${Anchor} href="./rollout-evidence.json"
            >Rollout checks and capacity model<//
          >
          <${Anchor} href="./evidence.json">Earlier evidence<//>
          <${Anchor} href="./manifest.json">Dates and hashes<//>
        <//>
      <//>
    </section>
  <//>`;
}

mount(Report);
