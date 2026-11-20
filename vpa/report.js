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
    fetch("./evidence.json?v=cnpg-20261120")
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(setData)
      .catch((e) => setError(String(e)));
  }, []);
  if (error)
    return html`<${Container} py="xl"
      ><${Alert} color="red" title="Evidence could not be loaded"
        >${error}<//
      ><//
    >`;
  if (!data)
    return html`<${Container} py="xl"><${Text}>Loading VPA evidence…<//><//>`;
  const counts = data.counts;
  const chart = [
    { mode: "Recommend", count: counts.vpa_modes.Off },
    { mode: "Initial", count: counts.vpa_modes.Initial },
    { mode: "Recreate", count: counts.vpa_modes.Recreate },
  ];
  return html`<${Container}
    component="main"
    size="78rem"
    py="xl"
    style=${{ overflowWrap: "anywhere" }}
  >
    <${Anchor} href="../">← All Ducktape reports<//>
    <header>
      <${Group} gap="sm" mt="md"
        ><${Badge} variant="light">Cluster audit<//
        ><${Badge} color="gray" variant="light">Snapshot, not live<//><//
      >
      <${Title} order=${1} mt="sm"
        >VPA coverage and a path to broader adoption<//
      >
      <${Text} size="lg" mt="md"
        >Use VPA for requests much more widely, but select the update mode and
        resource ownership per workload. Preserve existing request-and-limit
        policies unless evidence justifies changing them.<//
      >
      <${Text} size="sm" c="dimmed" mt="sm"
        >${`Cluster snapshot around 20 November 2026, 02:58 UTC · latest seven-day query ${data.history_end_utc} · inspected devel ${data.source_devel_commit}. Cluster state may have moved since collection.`}<//
      >
      <${Group} mt="sm" gap="md"
        ><${Anchor} href="./evidence.json">Evidence and inventory JSON<//
        ><${Anchor} href="./manifest.json">Manifest<//
        ><${Anchor} href="#batches">Recommended batches<//
        ><${Anchor} href="#inventory">Search inventory<//><//
      >
    </header>

    <${SimpleGrid} cols=${{ base: 1, sm: 2, lg: 4 }} mt="xl" spacing="md">
      <${Stat}
        value=${counts.all_vpa_objects}
        label="VPA objects"
        detail="Includes Jobs, CronJobs and stale targets; an object is not proof of an update."
      />
      <${Stat}
        value=${`${counts.direct_vpa_targets} / ${counts.controller_templates}`}
        label="Directly targeted controllers"
        detail="Deployments, StatefulSets and DaemonSets; 68 have no direct VPA target."
      />
      <${Stat}
        value=${`${counts.active_pod_resource_missing.requests_memory_missing} / ${counts.active_pod_containers}`}
        label="Active containers without memory requests"
        detail="Regular containers in Running or Pending pods after admission."
      />
      <${Stat}
        value=${`${data.cnpg.without_declared_memory_request} / ${data.cnpg.clusters}`}
        label="CNPG clusters without memory requests"
        detail="Custom-resource templates, not Deployment VPA targets."
      />
    <//>

    <${Alert}
      color="orange"
      title="Two current sizing problems deserve attention before a rollout"
      mt="xl"
    >
      <${Text} component="p" mb="xs"
        ><strong>tofu-controller:</strong> OOM-killed at 02:57 UTC with a 1 GiB
        limit and a 128 MiB request; a later sample was 944 MiB. Its namespace
        disables Goldilocks. Diagnose the memory growth and correct the limit
        and request separately; request-only VPA cannot raise that limit.<//
      >
      <${Text} component="p" mb=${0}
        ><strong>Attic:</strong> OOM-killed on 19 November with an 8 GiB limit;
        its VPA target was 8.52 GiB. Its current singleton has a roughly 5.7 GiB
        admitted request. Investigate memory and limit headroom; request-only
        mode is capped by the limit and the updater’s default two-replica guard
        blocks a singleton eviction.<//
      >
    <//>

    <section id="patterns">
      <${Title} order=${2} mt="xl">What is actually running<//>
      <${Text} component="p"
        >Goldilocks v4.16.1 creates VPA objects for supported controllers; VPA
        v1.7.1 computes recommendations and mutates pods at admission. The
        namespace label <${Code}>auto<//> becomes VPA
        <${Code}>Recreate<//> here. Namespace and workload annotations may
        override defaults; inspect the generated VPA, not just the label.<//
      >
      <${SimpleGrid} cols=${{ base: 1, md: 2 }} spacing="md" my="md">
        <${Paper} withBorder p="md" radius="md">
          <${Title} order=${3} size="h4">VPA objects by update mode<//>
          <${BarChart}
            h=${240}
            data=${chart}
            dataKey="mode"
            series=${[{ name: "count", color: "blue.6" }]}
            tickLine="none"
            gridAxis="y"
          />
          <${Text} size="sm" c="dimmed"
            >${counts.all_vpa_objects} objects total, including 28 Job/CronJob
            targets; this is not the count of active workloads.<//
          >
        <//>
        <${Paper} withBorder p="md" radius="md">
          <${Title} order=${3} size="h4">Policy and disruption<//>
          <${Text} component="p"
            ><strong>Recommend / Off (${counts.vpa_modes.Off}):</strong>
            ${" observations only; neither requests nor limits are changed."}<//
          >
          <${Text} component="p"
            ><strong>Initial (${counts.vpa_modes.Initial}):</strong>
            ${` changes apply at pod creation, with no VPA eviction. ${data.vpa_policies.Initial.noContainerPolicies} have no explicit container policy and therefore use the VPA default of requests and limits; 4 contain an explicit request-only policy.`}<//
          >
          <${Text} component="p" mb=${0}
            ><strong>Recreate (${counts.vpa_modes.Recreate}):</strong>
            ${" all 51 have at least one request-only policy. VPA may evict and recreate; the installed updater defaults to a minimum of 2 replicas unless a VPA overrides it. A singleton can still be resized on ordinary pod creation."}<//
          >
        <//>
      <//>
      <${Text} component="p"
        >The cluster’s Kyverno default injects <${Code}>RequestsOnly<//> for
        auto-labeled namespaces. It does <em>not</em> apply to Initial
        namespaces. Request-only leaves existing limits as declared; the default
        requests-and-limits policy scales existing limits proportionally.
        Missing limits stay a separate policy decision, not an instruction to
        create a hard cap. ${" "}
        <${Anchor} href=${source + "cluster/cdk8s/kyverno/policies.py#L267"}
          >Local policy and incident context<//
        >
        ·
        <${Anchor}
          href="https://github.com/kubernetes/autoscaler/blob/vertical-pod-autoscaler-1.7.1/vertical-pod-autoscaler/docs/faq.md"
          >VPA FAQ<//
        ><//
      >
    </section>

    <section id="coverage">
      <${Title} order=${2} mt="xl">Where requests are missing or misleading<//>
      <${Text} component="p"
        >${`Among ${counts.template_containers} regular containers in ${counts.controller_templates} Deployment, StatefulSet and DaemonSet templates, ${counts.template_resource_missing.requests_memory_missing} omit memory requests, ${counts.template_resource_missing.limits_memory_missing} omit memory limits, and ${counts.template_resource_missing.limits_cpu_missing} omit CPU limits. These are template fields, not admitted-pod settings; a missing CPU limit is often intentional.`}<//
      >
      <${Text} component="p"
        >${`Among ${counts.active_pod_containers} regular containers in ${counts.active_pods} Running or Pending pods, ${counts.active_pod_resource_missing.requests_memory_missing} have no admitted memory request and ${counts.active_pod_resource_missing.limits_memory_missing} have no memory limit. This includes ${data.cnpg.instance_containers_without_request} CNPG instance containers and 63 Piraeus containers; the classes are not a count of independently fixable services. Init/ephemeral containers are excluded. Metrics were available for ${counts.metrics_covered_active_pods}/${counts.active_pods} active pods.`}<//
      >
      <${Text} component="p"
        >Examples at the snapshot: the Piraeus LINSTOR controller had no request
        and used about 1.0 GiB; several satellites had no request and used
        roughly 240–390 MiB each. These are point-in-time working sets, not safe
        request recommendations. CDviz used about 203 MiB against a 128 MiB
        request; its 7-day observed peak for the current pod was 225 MiB, and
        VPA target was 259 MiB.<//
      >
      <${Text} component="p"
        >CNPG 1.30.1 exposes a working <${Code}>Cluster<//> scale selector, but
        Goldilocks has not created VPAs for these custom-resource targets. A
        namespace mode change therefore does not automatically cover PostgreSQL.
        Pilot an explicitly owned VPA in recommendation mode and verify
        admission and CNPG reconciliation; use operator-managed explicit
        requests where that interaction is unsuitable. ${" "}
        <${Anchor}
          href="https://github.com/cloudnative-pg/cloudnative-pg/issues/2574"
          >Upstream scale-selector feature<//
        ><//
      >
      <${Accordion} variant="separated" my="md">
        <${Accordion.Item} value="database-inventory">
          <${Accordion.Control}>CNPG database inventory · 23 clusters<//>
          <${Accordion.Panel}>
            <${Text} size="sm" c="dimmed" mb="sm">
              Cluster resource fields are operator inputs. Current memory is the
              largest sampled PostgreSQL instance working set, not a historical
              peak; backup sidecars are separate. Unset means no field was
              declared or admitted, not zero usage.
            <//>
            <${Table.ScrollContainer}
              minWidth=${820}
              type="native"
              tabIndex=${0}
              aria-label="CNPG database resource inventory"
            >
              <${Table} striped withTableBorder>
                <${Table.Thead}
                  ><${Table.Tr}>
                    <${Table.Th}>Namespace / cluster<//
                    ><${Table.Th}>Active / desired instances<//>
                    <${Table.Th}>Declared CPU request / limit<//
                    ><${Table.Th}>Declared memory request / limit<//>
                    <${Table.Th}>Active without memory request<//
                    ><${Table.Th}>Current max instance working set<//>
                  <//><//
                >
                <${Table.Tbody}>
                  ${(data.cnpg.cluster_details ?? []).map(
                    (c) =>
                      html`<${Table.Tr} key=${`${c.namespace}/${c.name}`}>
                        <${Table.Td}>${`${c.namespace} / ${c.name}`}<//>
                        <${Table.Td}
                          >${`${c.activeInstances} / ${c.desiredInstances}`}<//
                        >
                        <${Table.Td}
                          >${`${c.cpuRequest ?? "unset"} / ${c.cpuLimit ?? "unset"}`}<//
                        >
                        <${Table.Td}
                          >${`${c.memoryRequest ?? "unset"} / ${c.memoryLimit ?? "unset"}`}<//
                        >
                        <${Table.Td}>${c.activeWithoutRequest}<//>
                        <${Table.Td}
                          >${c.currentMaxWorkingSetMiB == null ? "unknown" : `${c.currentMaxWorkingSetMiB} MiB`}<//
                        >
                      <//>`,
                  )}
                <//>
              <//>
            <//>
          <//>
        <//>
      <//>
      <${Title} order=${3} size="h4" mt="lg"
        >Node reservations versus current use<//
      >
      <${Text} size="sm" c="dimmed" mb="sm"
        >Percent of allocatable. Requests come from kubectl’s
        allocated-resources summary; use is metrics-server working set at
        another instant. They are not forecasts or interchangeable free
        capacity. Placement, affinity, storage and transient peaks matter.<//
      >
      <${Table.ScrollContainer}
        minWidth=${520}
        type="native"
        tabIndex=${0}
        aria-label="Node memory reservations and use"
        ><${Table} striped withTableBorder>
          <${Table.Thead}
            ><${Table.Tr}
              ><${Table.Th}>Node<//><${Table.Th}>Ready<//
              ><${Table.Th}>Memory requested<//
              ><${Table.Th}>Current working set<//><//
            ><//
          >
          <${Table.Tbody}
            >${data.nodes.map((n) => html`<${Table.Tr} key=${n.name}><${Table.Td}>${n.name}<//><${Table.Td}>${n.ready}<//><${Table.Td}>${n.memoryRequestPercent == null ? "unknown" : `${n.memoryRequestPercent}%`}<//><${Table.Td}>${n.currentMemoryWorkingSetPercent == null ? "unknown" : `${n.currentMemoryWorkingSetPercent}%`}<//><//>`)}<//
          >
        <//><//
      >
      <${Text} component="p"
        >Three OVH nodes were at 81–83% requested while others had lower
        reservations. This does not establish that their headroom is usable by a
        particular pod. A previous Grocy scheduling investigation recorded 97%
        of HIL worker memory requested against 65–78% usage; that is historical,
        not the current snapshot. ${" "}
        <${Anchor} href=${source + "cluster/docs/plan.md#L166"}
          >Recorded investigation<//
        ><//
      >
    </section>

    <section id="incidents">
      <${Title} order=${2} mt="xl">Why the mode and limit policy matter<//>
      <${SimpleGrid} cols=${{ base: 1, md: 2 }} spacing="md">
        <${Paper} withBorder p="md" radius="md"
          ><${Title} order=${3} size="h4">Limit downsizing has broken startup<//
          ><${Text} component="p"
            >The local Kyverno policy records a LiteLLM outage:
            request-and-limit scaling reduced its CPU limit and both replicas
            failed startup for six hours. The policy now defaults auto
            namespaces to request-only. This is evidence for a targeted
            exception, not a reason to replace every existing
            requests-and-limits policy. ${" "}
            <${Anchor} href=${source + "cluster/cdk8s/kyverno/policies.py#L267"}
              >Policy rationale<//
            ><//
          ><//
        >
        <${Paper} withBorder p="md" radius="md"
          ><${Title} order=${3} size="h4">Limit growth has also helped<//
          ><${Text} component="p"
            >Forgejo’s source records VPA increasing a former 2 GiB limit above
            observed use; the configured limit was subsequently raised. Changing
            an existing policy to request-only can restore a lower template
            limit at the next creation. Preserve existing control unless usage,
            startup and recovery evidence supports a change. ${" "}
            <${Anchor} href=${source + "cluster/cdk8s/forgejo/app.py#L287"}
              >Forgejo sizing note<//
            ><//
          ><//
        >
        <${Paper} withBorder p="md" radius="md"
          ><${Title} order=${3} size="h4">Eviction can disrupt shared storage<//
          ><${Text} component="p"
            >VPA replacement of Forgejo pods refreshed stale SeaweedFS mounts
            during a past incident. It was an effective recovery, not evidence
            that periodic or concurrent evictions are safe. Replica topology,
            PDBs and storage behavior need review. ${" "}
            <${Anchor}
              href=${source + "cluster/docs/lessons_learned/2026_08_31_forgejo_seaweedfs_fuse_coherence_recurrence.md#recovery-and-current-limitation"}
              >Incident<//
            ><//
          ><//
        >
        <${Paper} withBorder p="md" radius="md"
          ><${Title} order=${3} size="h4">Reflector illustrates the upside<//
          ><${Text} component="p"
            >After an OOM at 256 MiB, its limit rose to 768 MiB. Its previous
            observed working-set peak was 425 MiB against a 128 MiB request.
            Request-only VPA now sizes the request and is allowed to restart its
            single replica; mirroring can pause during replacement. ${" "}
            <${Anchor} href=${github + "pull/17979"}>Deployed change<//><//
          ><//
        >
      <//>
    </section>

    <section id="batches">
      <${Title} order=${2} mt="xl">Recommended batches<//>
      <${Text} component="p"
        >These are proposed changes, not approvals or forecasts. Split
        independently reviewable changes into focused PRs. Preserve existing VPA
        limit ownership, resource bounds and workload overrides unless a
        separate investigation justifies changing them.<//
      >
      <${Stack} gap="sm">
        <${Paper} withBorder p="md" radius="md"
          ><${Badge} color="red" variant="light">0 · immediate diagnosis<//
          ><${Title} order=${3} size="h4" mt="xs"
            >Fix the two OOMing limits on their own merits<//
          ><${Text} component="p" mb=${0}
            >Investigate tofu-controller’s recent 1 GiB OOM and Attic’s 8 GiB
            OOM, memory trajectory and appropriate limits/requests. For Attic,
            the 8.52 GiB target cannot be fully admitted below its 8 GiB limit.
            For tofu-controller, decide whether a scoped VPA is safe for the
            GitOps control plane only after restoring headroom and understanding
            the cause. Keep limit repairs separate from the adoption rollout.<//
          ><//
        >
        <${Paper} withBorder p="md" radius="md"
          ><${Badge} color="blue" variant="light">1 · Initial pilots<//
          ><${Title} order=${3} size="h4" mt="xs"
            >CDviz, then carefully selected controllers<//
          ><${Text} component="p" mb=${0}
            >Start with CDviz collector in request-only Initial mode: its
            current request is below observed use, and VPA would not
            deliberately restart its singleton webhook receiver. The namespace
            also contains a retention CronJob; verify its VPA behavior and
            exclude it if needed. Next assess external-dns and trust-manager
            individually, including node fit, startup and rollback. GitHub does
            not automatically redeliver failed webhook deliveries, so do not
            make CDviz auto-evictable until delivery recovery is verified.
            ${" "}
            <${Anchor}
              href="https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/redelivering-webhooks"
              >GitHub delivery behavior<//
            ><//
          ><//
        >
        <${Paper} withBorder p="md" radius="md"
          ><${Badge} color="blue" variant="light">2 · automatic pilots<//
          ><${Title} order=${3} size="h4" mt="xs"
            >Expand Recreate where interruption is acceptable<//
          ><${Text} component="p" mb=${0}
            >The GitLab acceptance demo is already a request-only Recreate
            canary; observe its replacements and cap behavior. After an Initial
            pilot, external-dns is a candidate if a short reconciliation gap is
            acceptable. Assess the GitLab spike exporter and runner separately,
            including whether restarting the runner affects in-flight jobs. The
            exporter shares a namespace with HPAs and stateful services: use a
            workload-scoped mode and explicit resource policy, not a blanket
            namespace switch. Kyverno's request-only default matches the
            namespace label, not an individual workload override. For
            singletons, explicitly decide whether to retain the default
            two-replica eviction guard or permit downtime with
            <${Code}>minReplicas: 1<//>; Goldilocks applies that setting at
            namespace scope, so inspect every affected VPA. For replicas, check
            PDBs, scheduling and dependency readiness before evictions.<//
          ><//
        >
        <${Paper} withBorder p="md" radius="md"
          ><${Badge} color="cyan" variant="light">3 · custom operators<//
          ><${Title} order=${3} size="h4" mt="xs"
            >Pilot CNPG sizing without automatic failover<//
          ><${Text} component="p" mb=${0}
            >The installed CNPG version exposes the scale selector, making a
            custom-target VPA plausible; it does not prove Goldilocks creates
            one or that admission, reconciliation, backup sidecars and failover
            are safe. Use a disposable or explicitly chosen cluster with a
            separately owned Off VPA, then test Initial request-only admission
            and operator reconciliation. Compare explicit CNPG resource requests
            as the fallback. Do not mass-enable database evictions.<//
          ><//
        >
        <${Paper} withBorder p="md" radius="md"
          ><${Badge} color="cyan" variant="light"
            >4 · infrastructure and dynamic workloads<//
          ><${Title} order=${3} size="h4" mt="xs"
            >Measure Piraeus, CSI, Flux, agent workloads and VMs separately<//
          ><${Text} component="p" mb=${0}
            >Piraeus and CSI include many no-request DaemonSet containers and
            node-specific components; add evidence-based requests at the owning
            operator or pilot narrowly after proving recovery. Review disabled
            Agentplane and platform namespaces one workload at a time.
            Sandboxes, Terraform runners, KubeVirt VMs, short-lived Jobs and
            unobserved startup peaks need explicit ownership and sizing
            experiments rather than inferred coverage from a namespace
            toggle.<//
          ><//
        >
        <${Paper} withBorder p="md" radius="md"
          ><${Badge} color="gray" variant="light">guardrail · HPA<//
          ><${Title} order=${3} size="h4" mt="xs"
            >Keep CPU HPA and CPU VPA from competing<//
          ><${Text} component="p" mb=${0}
            >The GitLab spike has four CPU HPAs; the registry uses CPU
            utilization, which depends directly on the request. Consider
            memory-only VPA for those workloads, leaving CPU to HPA, and test
            any CPU-based combination against upstream guidance. The separate
            KEDA VM pool uses an external metric and needs its own
            operator-specific assessment. ${" "}
            <${Anchor}
              href="https://github.com/kubernetes/autoscaler/blob/vertical-pod-autoscaler-1.7.1/vertical-pod-autoscaler/docs/known-limitations.md"
              >VPA limitations<//
            ><//
          ><//
        >
      <//>
      <${Text} component="p" mt="md"
        >For every batch: record the old and new effective request and limit,
        current and historical usage, peak/startup uncertainty, node/zone fit,
        HPA and PDB constraints; verify the generated VPA and a normally created
        pod after rollout. Watch scheduling failures, restarts, throttling and
        application health; revert the declarative setting if the result is
        unsafe. <${Code}>Initial<//> is not an immediate fix for an already
        running under-requested pod.<//
      >
    </section>

    <section id="inventory">
      <${Title} order=${2} mt="xl">Search the workload inventory<//>
      <${Text} component="p"
        >${data.workloads.length} Deployment, StatefulSet, DaemonSet and CronJob
        templates. Each row joins an exact VPA target reference where present;
        no direct target does not prove that no other policy mutates a pod.
        Recommendations are raw VPA targets and can be capped at admission. A
        seven-day peak covers only current pods in sampled namespaces; a newly
        created pod has a shorter effective window. Missing telemetry is shown
        as unknown.<//
      >
      <${Inventory} workloads=${data.workloads} />
    </section>

    <section id="method">
      <${Title} order=${2} mt="xl">Methods and limits<//>
      <${Text} component="p"
        >Read-only Kubernetes API snapshots cover 197 VPAs, controller
        templates, active pod specifications, HPAs, PDBs, nodes and
        metrics-server; selected namespaces have seven-day Mimir container
        working-set peaks. The 30-day VolSync value is maximum total container
        memory, not working set. Metrics are sampled, do not capture every spike
        and do not prove the cause of an OOM. API observations were collected
        over several minutes; the inspected source revision is not proof every
        live object was reconciled from that commit.<//
      >
      <${Text} component="p"
        >VPA objects targeting completed Jobs may persist. Direct target
        matching does not establish admission success, target selector support
        or actual evictions. CronJobs, CNPG, Sandbox, Terraform and VM
        controllers need separate ownership and admission checks.
        Instance/replica count can change. Limits, requests and recommendations
        are distinct; the scheduler uses admitted requests and placement
        constraints, not today’s usage. No cluster resources were changed to
        produce this report.<//
      >
      <${Group} gap="md" my="sm"
        ><${Anchor}
          href="https://github.com/kubernetes/autoscaler/blob/vertical-pod-autoscaler-1.7.1/vertical-pod-autoscaler/docs/quickstart.md"
          >VPA v1.7.1 modes<//
        >
        <${Anchor}
          href="https://github.com/kubernetes/autoscaler/blob/vertical-pod-autoscaler-1.7.1/vertical-pod-autoscaler/docs/known-limitations.md"
          >Known limitations<//
        >
        <${Anchor}
          href="https://kubernetes.io/docs/concepts/workloads/pods/pod-qos/"
          >Pod QoS and eviction<//
        >
        <${Anchor} href=${source + "cluster/cdk8s/namespaces.py"}
          >Namespace configuration<//
        >
        <${Anchor} href=${github + "issues/17987"}>ntfy replica follow-up<//><//
      >
      <${Text} size="sm" c="dimmed"
        >Evidence contains reviewed resource fields and aggregate measurements,
        not credentials, logs or raw Kubernetes objects.<//
      >
    </section>
  <//>`;
}

mount(Report);
