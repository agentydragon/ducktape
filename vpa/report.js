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
const designSource =
  "https://github.com/agentydragon/ducktape/blob/d81bd68165097b089cace9ae168b6e8f202f1b7e/";
const github = "https://github.com/agentydragon/ducktape/";
const currentSource = github + "blob/2aa028749c5a77cfa56c26abd1242d86b44bd12b/";
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

const cutoverProof = {
  "default-vpa-requests-only":
    "Cover externally authored namespaces and workload overrides; compare effective policies before retiring the admission fallback.",
  "default-revision-history-limit":
    "Post-prune dry runs confirmed the default of 10, rejection of negative Deployment and DaemonSet input, and unchanged negative StatefulSet input.",
  "default-disable-service-links":
    "Preserve the Pod CREATE behavior for raw and operator-created Pods, including explicit true and excluded namespaces.",
  "require-gitops":
    "Prove accessible native events and suitable retention before retiring the PolicyReport pipeline.",
  "restrict-agent-gateway-routes":
    "After pruning Kyverno, native admission denied all 20 sandbox dry runs and allowed all 10 operator creates and the operator update.",
  "require-secret-store-conditions":
    "Keep enforcement for private and public sources and scanning of stored objects until a supported replacement covers both.",
  "agentplane-staging-launcher-relay":
    "Preserve fail-closed owner and credential checks, mutation, protected mounts, VM lifecycle and ephemeral-container update protection.",
};

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
    fetch("./evidence.json?v=retirement-20261120-1134")
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(setData)
      .catch((e) => setError(String(e)));
  }, []);
  useEffect(() => {
    // Evidence can arrive after the browser's initial fragment lookup.
    const fragment = window.location.hash.slice(1);
    if (data && fragment) document.getElementById(fragment)?.scrollIntoView();
  }, [data]);
  if (error)
    return html`<${Container} py="xl"
      ><${Alert} color="red" title="Evidence could not be loaded"
        >${error}<//
      ><//
    >`;
  if (!data)
    return html`<${Container} py="xl"><${Text}>Loading VPA evidence…<//><//>`;
  const counts = data.counts;
  const latest = data.third_cohort;
  const followup = data.latest_rollout_followup;
  const followupTime = followup.observed_at_utc.slice(11, 16);
  const wave = data.latest_rollout_wave;
  const waveTime = wave.observed_at_utc.slice(11, 16);
  const progress = data.retirement_progress;
  const progressTime = progress.observed_at_utc.slice(11, 16);
  const cutover = data.gateway_cutover;
  const cutoverTime = cutover.observed_at_utc.slice(11, 16);
  const nativeGatewayPolicy = "restrict-agent-gateway-routes";
  const remainingPolicies = data.remaining_policy_review;
  const remainingPoliciesTime = remainingPolicies.reviewed_at_utc.slice(11, 16);
  const secretStoreDecision = data.secret_store_retirement_decision;
  const secretStoreDecisionTime = secretStoreDecision.decided_at_utc.slice(
    11,
    16,
  );
  const certManagerHA = data.cert_manager_ha;
  const certManagerTime = certManagerHA.observed_at_utc.slice(11, 16);
  const controllerHA = data.controller_ha_followup;
  const controllerHATime = controllerHA.observed_at_utc.slice(11, 16);
  const crossplaneHA = controllerHA.crossplane;
  const metricsHA = controllerHA.metrics_server;
  const sidekiq = controllerHA.sidekiq;
  const crossplaneSiteSpread = data.crossplane_site_spread;
  const stagingInitial = data.agentplane_staging_initial;
  const stagingInitialTime = stagingInitial.observed_at_utc.slice(11, 16);
  const stagingControllers = data.agentplane_staging_controllers;
  const secretStoreRecovery = data.secret_store_background_recovery;
  const webFrontendHA = data.web_frontend_ha;
  const webFrontendHATime = webFrontendHA.observed_at_utc.slice(11, 16);
  const clickhouseExporter = data.clickhouse_exporter_initial;
  const clickhouseExporterTime = clickhouseExporter.observed_at_utc.slice(
    11,
    16,
  );
  const stagingAdmissionTime =
    stagingInitial.admission_verification.observed_at_utc.slice(11, 16);
  const revisionHistoryCutover = data.revision_history_cutover;
  const revisionHistoryTime = revisionHistoryCutover.observed_at_utc.slice(
    11,
    16,
  );
  const policyState = {
    [nativeGatewayPolicy]: "Retired",
    "require-gitops": "Audit delivery gate",
    "require-secret-store-conditions": "Retain Kyverno",
    "default-revision-history-limit": "Retired",
  };
  const policyDirection = {
    [nativeGatewayPolicy]: "Native ValidatingAdmissionPolicy and Deny binding.",
    "require-gitops": "Native Audit-only shadow alongside PolicyReports.",
    "default-revision-history-limit":
      "Kubernetes built-in defaults and validation.",
    "require-secret-store-conditions":
      "Keep Kyverno admission and background scanning. Supported replacement undecided.",
  };
  const latestTime = latest.observed_at_utc.slice(11, 16);
  const verifiedCount = latest.workloads.filter(
    (row) => row.dry_run_admitted_resources,
  ).length;
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
        resource ownership by workload class. Roll out shared policies in
        cohorts, preserving existing request-and-limit policies unless evidence
        justifies changing them.<//
      >
      <${Text} size="sm" c="dimmed" mt="sm"
        >${`Cluster snapshot around 20 November 2026, 02:58 UTC · latest seven-day query ${data.history_end_utc} · inspected devel ${data.source_devel_commit}. Rollout checks at 09:10, 09:21, ${latestTime}, ${followupTime}, ${waveTime}, ${progressTime}, ${cutoverTime} and ${revisionHistoryTime} UTC keep their own observations. Inventory and charts use the original snapshot.`}<//
      >
      <${Group} mt="sm" gap="md"
        ><${Anchor} href="./evidence.json">Evidence and inventory JSON<//
        ><${Anchor} href="./manifest.json">Manifest<//
        ><${Anchor} href="#architecture">Policy refactor<//
        ><${Anchor} href="#kyverno-retirement">Kyverno policies<//
        ><${Anchor} href="#remaining-work">Remaining work<//
        ><${Anchor} href="#batches">Rollout evidence<//
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
      title="Earlier OOMs: historical evidence and current follow-up"
      mt="xl"
    >
      <${Text} component="p" mb="xs"
        ><strong>tofu-controller:</strong> OOM-killed at 02:57 UTC with a 1 GiB
        limit and a 128 MiB request; a later sample was 944 MiB. Its namespace
        disabled Goldilocks. The former controller was race-instrumented and
        processed an oversized private source artifact; the private root moved
        to Crossplane and the controller switched to upstream v0.17.0. The new
        pod had zero restarts at 08:24 UTC; keep monitoring. Request-only VPA
        cannot raise a memory limit.<//
      >
      <${Text} component="p" mb=${0}
        ><strong>Attic:</strong> OOM-killed on 19 November with an 8 GiB limit;
        its VPA target was 8.52 GiB. Its singleton had a roughly 5.7 GiB
        admitted request. Follow the existing Attic tracking issue; do not start
        a duplicate investigation. Request-only mode is capped by the limit and
        the updater’s default two-replica guard blocks a singleton eviction.<//
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
      <${Text} component="p">
        cdk8s now renders the <${Code}>RequestsOnly<//> default for auto-mode
        namespaces, and the corresponding Kyverno policy has been removed. This
        default does not apply to Initial namespaces. Existing explicit
        policies, including <${Code}>RequestsAndLimits<//>, take precedence and
        should not be silently weakened. Request-only leaves existing limits as
        declared; missing limits are a separate decision. ${" "}
        <${Anchor} href=${currentSource + "cluster/cdk8s/namespaces.py"}>
          Namespace defaults<//
        >
        ${" · "}<${Anchor} href=${github + "pull/18085"}>Kyverno removal<//>
      <//>
    </section>

    <section id="architecture">
      <${Title} order=${2} mt="xl"
        >Define VPA behavior once; simplify policies where useful<//
      >
      <${Text} size="sm" c="dimmed">
        ${`Architecture review: ${data.architecture_review.observed_at_utc}; devel ${data.architecture_review.source_devel_commit}. The workload and metrics snapshot above remains from 02:58 UTC.`}
      <//>
      <${Text} component="p">
        Expand VPA request coverage and author common behavior in cdk8s while
        keeping Goldilocks as the owner of VPAs it generates. Retain Kyverno
        where it is the simpler way to provide admission, background reporting,
        or cleanup. Replace individual policies only when the alternative is
        simpler overall and preserves the behavior we need; do not build custom
        scanners merely to remove Kyverno. Shared VPA profile and memory-policy
        authoring are complete, and the Kyverno VPA fallback is removed.
      <//>
      <${Title} order=${3} size="h4" mt="md"
        >Small profiles, explicit exceptions<//
      >
      <${Text} component="p">
        The merged <${Code}>VpaProfile<//> and
        <${Code}>ContainerPolicy<//> helpers express modes, request/limit
        ownership, bounds and exclusions. The shared memory-request helper
        replaced repeated workload code with byte-identical generated manifests.
        ${" "}<${Anchor} href=${github + "pull/18132"}
          >Shared memory policies<//
        >
        ${" "}Apply shared profiles at the namespace level where every workload
        is eligible; use workload-level overrides or split namespaces where they
        are not. Preserve policies that already control limits. ${" "}
        <${Anchor} href=${currentSource + "cluster/cdk8s/namespaces.py"}>
          Namespace profiles<//
        >
        ${" · "}<${Anchor}
          href=${currentSource + "cluster/cdk8s/vpa_policy.py"}
        >
          Container policies<//
        >
      <//>
      <${Text} component="p" mt="sm">
        An eligible new service can choose Initial request-only in one
        expression; promote it to Recreate when eviction is acceptable. Preserve
        existing request-and-limit policies unless a specific workload is
        reviewed. A workload-level resource policy replaces the namespace policy
        in Goldilocks, so the builder must emit a complete policy, including the
        intended controlled values, rather than silently dropping them.
        ${" "}<${Anchor}
          href="https://github.com/FairwindsOps/goldilocks/blob/v4.16.1/pkg/vpa/vpa.go#L209-L219"
          >Goldilocks precedence<//
        >
      <//>
      <${Text} component="p">
        Keep singleton eviction opt-in. In the installed Goldilocks v4.16.1,
        <${Code}>minReplicas<//> comes from the namespace, so a request for one
        evictable singleton must review the effect on every VPA there. Do not
        promise a per-workload override that Goldilocks will ignore. For custom
        targets such as CNPG, pilot a separately owned VPA and prove there is no
        duplicate target or controller conflict. ${" "}<${Anchor}
          href="https://github.com/FairwindsOps/goldilocks/blob/v4.16.1/pkg/vpa/vpa.go#L159-L162"
          >Namespace minimum<//
        >
      <//>
      <${Title} order=${3} size="h4" mt="md">Migration sequence<//>
      <${Table.ScrollContainer}
        minWidth=${720}
        type="native"
        tabIndex=${0}
        aria-label="VPA configuration migration sequence"
      >
        <${Table} striped withTableBorder>
          <${Table.Thead}
            ><${Table.Tr}
              ><${Table.Th}>Step<//><${Table.Th}>Change<//
              ><${Table.Th}>Gate<//><//
            ><//
          >
          <${Table.Tbody}>
            <${Table.Tr}
              ><${Table.Td}>1 · Done: profiles<//
              ><${Table.Td}
                >Typed profile and annotation builders landed in #18021. Shared
                memory policy adoption landed in #18132 with identical generated
                manifests.<//
              ><${Table.Td}
                >Compare rendered metadata and effective VPA specs; preserve
                modes, values, overrides and single ownership.<//
              ><//
            >
            <${Table.Tr}
              ><${Table.Td}>2 · In progress: adoption<//
              ><${Table.Td}
                >Initial cohorts and post-recommendation admission checks are
                recorded below. GitLab Shell, the External Secrets controller
                and Kyverno admission are in Recreate. Review remaining
                candidates against the rollout gates.<//
              ><${Table.Td}
                >Check admission, HPA, node fit, bounds and disruption; compare
                actual pod resources after a normal creation.<//
              ><//
            >
            <${Table.Tr}
              ><${Table.Td}>3 · Done: VPA fallback<//
              ><${Table.Td}
                >cdk8s now renders the auto-mode default; Kyverno fallback
                removed in #18085.<//
              ><${Table.Td}
                >Existing auto policies matched before and after; new namespaces
                outside cdk8s must supply their own policy. Never
                blanket-default existing Initial VPAs to requests-only.<//
              ><//
            >
            <${Table.Tr}
              ><${Table.Td}>4 · Keep or simplify Kyverno policies<//
              ><${Table.Td}
                >Retain Kyverno for existing admission, background reporting,
                dynamic injection and cleanup unless a simpler replacement
                exists.<//
              ><${Table.Td}
                >Prove any simpler replacement on real admission and cleanup
                paths, including private sources and failure behavior, before
                disabling the corresponding policy.<//
              ><//
            >
          <//>
        <//>
      <//>
      <${Title} id="kyverno-retirement" order=${3} size="h4" mt="lg"
        >Kyverno policy status<//
      >
      <${Text} component="p">
        The 03:36 UTC inventory found seven ClusterPolicies and two namespaced
        CleanupPolicies. Removing the VPA default left six ClusterPolicies at
        11:00 UTC. The Gateway cutover left five at ${cutoverTime} UTC. After
        revision-history retirement, four ClusterPolicies and two
        CleanupPolicies were live at ${revisionHistoryTime} UTC.
        Operator-created Pods still need coverage from their owner or admission.
      <//>
      <${Table.ScrollContainer}
        minWidth=${980}
        type="native"
        tabIndex=${0}
        aria-label="Kyverno retirement inventory"
      >
        <${Table} striped withTableBorder verticalSpacing="sm">
          <${Table.Thead}
            ><${Table.Tr}
              ><${Table.Th}>Policy<//><${Table.Th} miw=${105}>State<//
              ><${Table.Th}>Current responsibility<//
              ><${Table.Th}>Candidate direction<//
              ><${Table.Th}>Proof or remaining gate<//><//
            ><//
          >
          <${Table.Tbody}>
            ${data.architecture_review.kyverno.policies
              .filter((p) => p.name !== "default-vpa-requests-only")
              .map(
                (p) =>
                  html`<${Table.Tr} key=${p.name}>
                    <${Table.Td}><${Code}>${p.name}<//><//
                    ><${Table.Td}>${policyState[p.name] ?? "Open"}<//
                    ><${Table.Td}>${p.role}<//
                    ><${Table.Td}>${policyDirection[p.name] ?? p.direction}<//
                    ><${Table.Td}>${cutoverProof[p.name]}<//>
                  <//>`,
              )}
            <${Table.Tr}>
              <${Table.Td}
                ><${Code}>sandbox-janitor<//> / <${Code}>reap-wedged-jobs<//><//
              >
              <${Table.Td}>Open<//>
              <${Table.Td}
                >claude-sandbox reaps six resource kinds older than seven days
                hourly. thrive-scraper reaps active Jobs older than six hours
                every 30 minutes.<//
              >
              <${Table.Td}
                >Keep the current policies until a supported replacement covers
                both.<//
              >
              <${Table.Td}
                >Preserve the schedules, kinds, age thresholds, predicates and
                delete permissions. Job TTL only handles finished Jobs.<//
              >
            <//>
          <//>
        <//>
      <//>
      <${Text} component="p">
        The Gateway cutover completed at ${cutoverTime} UTC. Flux pruned the
        Kyverno rule and the native Deny binding covered all ten served
        kind/version pairs in both sandbox namespaces. Operator creates and an
        update dry run passed. Both sandboxes held zero matching objects.
        ${" "}<${Anchor} href=${progress.prs[18153].html_url}
          >Native Gateway policy<//
        >
        ${" · "}<${Anchor} href=${progress.prs[18156].html_url}
          >Kyverno removal<//
        >
      <//>
      <${Text} component="p">
        Flux pruned the Kyverno revision-history rule. Post-prune dry runs at
        ${revisionHistoryTime} UTC confirmed that omitted values still become 10
        on create and update for Deployments, DaemonSets and StatefulSets.
        Kubernetes now rejects an explicit −1 for Deployments and DaemonSets. It
        accepts −1 for StatefulSets, whose controller retains unlimited history
        with a negative limit. The removed rule had rewritten −1 to 3 for all
        three kinds. ${" "}<${Anchor} href=${revisionHistoryCutover.html_url}
          >Revision-history retirement<//
        >
        ${" · "}<${Anchor}
          href=${revisionHistoryCutover.statefulset_negative_history_source}
          >StatefulSet behavior<//
        >
      <//>
      <${Text} component="p">
        At ${remainingPoliciesTime} UTC the remaining-policy review confirmed
        that Service links are forced off on Pod CREATE in the default namespace
        even when the caller explicitly requests true. In the excluded
        kube-system namespace the default is true and explicit values survive.
        Chart defaults cover their own Pod templates. Dynamic Pods still need
        admission coverage.
      <//>
      <${Text} component="p">
        The native GitOps Audit draft keeps Kyverno reporting and the existing
        exemptions. It waits for an accessible API audit consumer that can
        retrieve workload CREATE and UPDATE events. The three API servers have
        bounded local logs; the 11:24 review recorded 120 failed results in 304
        namespaced PolicyReports. It also identified launcher relay and cleanup
        parity work. ${" "}<${Anchor}
          href=${remainingPolicies.native_gitops_audit.html_url}
          >Gated native GitOps audit<//
        >
      <//>
      <${Text} component="p">
        The separate ClusterSecretStore audit proposal closed at
        ${secretStoreDecisionTime} UTC. Kyverno continues to enforce the rule
        and scan stored objects in the background. Both stay in place unless an
        existing, simpler solution preserves the required coverage. A custom
        background scanner is not the intended replacement. ${" "}<${Anchor}
          href=${secretStoreDecision.html_url}
          >Closed audit proposal<//
        >
      <//>
      <${Paper}
        id="secret-store-background-recovery"
        withBorder
        p="md"
        radius="md"
      >
        <${Title} order=${3} size="h4"
          >ClusterSecretStore background reporting<//
        >
        <${Text} size="sm" c="dimmed" mt="xs">
          Observed 20 November 2026 at 20:16 UTC
        <//>
        <${Text} component="p" mt="sm" mb=${0}>
          The legacy rule moved to a Kyverno ValidatingPolicy in
          ${" "}<${Anchor} href=${secretStoreRecovery.migration_pr}>PR #18199<//
          >. Only two of 60 stores retained policy reports while Kyverno's
          background resource discovery failed on the wildcard API version.
          ${" "}<${Anchor} href=${secretStoreRecovery.fix_pr}>PR #18249<//>
          ${" "}pinned the only served version, v1. Generation 2 was Ready and
          all 60 current store UIDs had fresh background pass results at
          20:16:00–01 UTC. The latest wildcard error was at 20:01, before the
          fix. A separate native admission policy denied an unfenced store in a
          server-side dry run; that does not establish which policy would deny
          it independently. The obsolete retirement draft ${" "}<${Anchor}
            href=${secretStoreRecovery.closed_draft_pr}
            >#18172<//
          >${" "}was closed without merging. The next hourly scan remains to be
          observed.
        <//>
      <//>
      <${Text} component="p">
        The 11:24 launcher review is historical. ${" "}<${Anchor}
          href="https://github.com/agentydragon/ducktape/pull/18231"
          >PR #18231<//
        >${" "}moved launcher validation to a Kyverno ValidatingPolicy, observed
        Ready; three mutation rules remain in the legacy ClusterPolicy. This
        snapshot does not establish fresh end-to-end launcher parity.
      <//>
      <${Text} component="p">
        The API server is v1.35.1 and already advertises native
        ValidatingAdmissionPolicy. Native MutatingAdmissionPolicy was not
        advertised in this snapshot; assess feature gates or an upgrade before
        relying on it. Native admission validation does not by itself reproduce
        Kyverno’s background scan. The launcher relay also reads live owner and
        credential objects and injects into KubeVirt-created Pods, so a static
        cdk8s rewrite alone cannot replace it. ${" "}<${Anchor}
          href="https://kubernetes.io/docs/reference/access-authn-authz/validating-admission-policy/"
          >Kubernetes validation<//
        >
        ${" · "}<${Anchor}
          href="https://kubernetes.io/docs/reference/access-authn-authz/mutating-admission-policy/"
          >Mutation API<//
        >
        ${" · "}<${Anchor}
          href=${designSource + "cluster/cdk8s/agentplane/launcher_relay.py"}
          >Launcher policy source<//
        >
      <//>
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
            >The earlier Kyverno policy recorded a LiteLLM outage:
            request-and-limit scaling reduced its CPU limit and both replicas
            failed startup for six hours. The cdk8s default now makes auto
            namespaces request-only. This is evidence for a targeted exception,
            not a reason to replace every existing requests-and-limits policy.
            ${" "}
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
      <${Title} order=${2} mt="xl">Rollout batches<//>
      <${Text} component="p">
        The goal is requests for almost all supported workloads, with automatic
        resizing wherever disruption is acceptable. Do not serialize the rollout
        one Deployment at a time: select cohorts by ownership and risk, use the
        shared profile once per namespace or chart, and review the effective
        policies together. The rollout tables use later, workload-specific
        observations than the inventory and coverage charts.
      <//>
      <${Alert} color="orange" title="Reconciliation is a rollout gate" my="md">
        Goldilocks watches Pods and Namespaces, not Deployment metadata. CDviz’s
        annotations landed after its last reconciliation and its VPA stayed Off
        until a namespace update at 08:17 UTC. Verify every intended VPA after
        each batch and trigger a namespace update when needed. GitLab spike also
        required a namespace update in the third cohort. CDviz’s real-owner dry
        run at 10:37 UTC confirmed Initial memory admission from 128 MiB to
        about 259 MiB, preserving 100m CPU, its 512 MiB limit and init
        resources. ${" "}
        <${Anchor}
          href="https://github.com/FairwindsOps/goldilocks/blob/v4.16.1/pkg/controller/controller.go"
        >
          Goldilocks watchers<//
        >
      <//>
      <${Stack} gap="sm">
        <${Paper} withBorder p="md" radius="md">
          <${Badge} color="green" variant="light"
            >1 · deployed Initial cohort<//
          >
          <${Title} order=${3} size="h4" mt="xs">
            Nine Deployments: rollout and admission verified
          <//>
          <${Text} component="p">
            All nine VPAs in external-dns, cert-manager, KEDA and
            external-secrets are Initial and the Deployments were ready at 09:10
            UTC. A server-side CREATE dry run using each running pod’s actual
            ReplicaSet owner invoked the VPA webhook. All nine produced the
            intended requests and retained their existing limits; no pods were
            created or evicted. These checks do not prove future scheduling or
            startup. The External Secrets webhook intentionally gains a 15m CPU
            request; the other eight retain their existing CPU requests.
            ${" "}<${Anchor} href=${github + "pull/18101"}>external-dns<//>
            ${" · "}<${Anchor} href=${github + "pull/18103"}>KEDA<//>
            ${" · "}<${Anchor} href=${github + "pull/18104"}>cert-manager<//>
            ${" · "}<${Anchor} href=${github + "pull/18105"}
              >External Secrets<//
            >
          <//>
          <${Table.ScrollContainer}
            minWidth=${800}
            type="native"
            tabIndex=${0}
            aria-label="First cohort admission dry runs"
          >
            <${Table} striped withTableBorder verticalSpacing="sm">
              <${Table.Thead}
                ><${Table.Tr}>
                  <${Table.Th}>Namespace / Deployment<//>
                  <${Table.Th}>Ready<//>
                  <${Table.Th}>Memory request: running → dry run<//>
                  <${Table.Th}>CPU request: running → dry run<//>
                <//><//
              >
              <${Table.Tbody}>
                ${data.rollout_progress.cohort.map((row) => {
                  const current = row.current_pod_resources[0].requests;
                  const admitted = row.dry_run_admitted_resources[0].requests;
                  const memory = (value) =>
                    value == null ? "unset" : quantityMiB(value);
                  return html`<${Table.Tr}
                    key=${`${row.namespace}/${row.deployment}`}
                  >
                    <${Table.Td}>${`${row.namespace} / ${row.deployment}`}<//>
                    <${Table.Td}
                      >${`${row.ready_replicas}/${row.desired_replicas}`}<//
                    >
                    <${Table.Td}
                      >${`${memory(current.memory)} → ${memory(admitted.memory)}`}<//
                    >
                    <${Table.Td}
                      >${`${current.cpu ?? "unset"} → ${admitted.cpu ?? "unset"}`}<//
                    >
                  <//>`;
                })}
              <//>
            <//>
          <//>
          <${Text} size="sm" c="dimmed" mt="sm" mb=${0}>
            Running-pod requests and admission results are a 09:10 UTC sample,
            not a live view. Limits in the dry-run output matched the running
            pod.
          <//>
        <//>
        <${Paper} withBorder p="md" radius="md">
          <${Badge} color="green" variant="light"
            >2 · deployed Initial cohort<//
          >
          <${Title} order=${3} size="h4" mt="xs">
            Node Feature Discovery and Kyverno: admission verified
          <//>
          <${Text} component="p" mb=${0}>
            Both service-family PRs are merged: all seven VPAs are Initial and
            all 17 replicas were ready at 09:21 UTC. Server-side CREATE dry runs
            using the actual ReplicaSet or DaemonSet owners invoked VPA for each
            workload; CPU requests and limits stayed unchanged. No pods were
            created or evicted, and actual scheduling and startup remain to be
            observed. The Kyverno migration Job VPA remains Off; Goldilocks
            picked up the Deployment annotations without a manual trigger.
            ${" "}<${Anchor}
              href=${data.second_cohort.node_feature_discovery_pr}
              >Node Feature Discovery PR<//
            >
            ${" · "}<${Anchor} href=${data.second_cohort.kyverno_pr}
              >Kyverno PR<//
            >
            ${" · "}<${Anchor}
              href="https://github.com/kubernetes/autoscaler/blob/vertical-pod-autoscaler-1.7.1/vertical-pod-autoscaler/pkg/target/fetcher.go#L111-L150"
              >DaemonSet selector handling<//
            >
          <//>
          <${Table.ScrollContainer}
            minWidth=${1000}
            type="native"
            tabIndex=${0}
            aria-label="Second cohort VPA bounds and admission dry runs"
          >
            <${Table} striped withTableBorder verticalSpacing="sm">
              <${Table.Thead}
                ><${Table.Tr}>
                  <${Table.Th}>Namespace / workload<//
                  ><${Table.Th}>Kind / replicas<//>
                  <${Table.Th}>Memory request floor–ceiling<//
                  ><${Table.Th}>Running → dry run<//
                  ><${Table.Th}>CPU request<//>
                <//><//
              >
              <${Table.Tbody}>
                ${data.second_cohort.workloads.map((row) => {
                  const actual = data.second_cohort.rollout.cohort.find(
                    (item) =>
                      item.namespace === row.namespace &&
                      item.workload === row.name,
                  );
                  const current = actual.current_pod_resources[0].requests;
                  const admitted =
                    actual.dry_run_admitted_resources[0].requests;
                  return html`<${Table.Tr}
                    key=${`${row.namespace}/${row.name}`}
                  >
                    <${Table.Td}>${`${row.namespace} / ${row.name}`}<//>
                    <${Table.Td}
                      >${`${row.kind} / ${actual.ready_replicas}/${actual.desired_replicas}`}<//
                    >
                    <${Table.Td}
                      >${`${row.memory_floor}–${row.memory_ceiling}`}<//
                    >
                    <${Table.Td}
                      >${`${quantityMiB(current.memory)} → ${quantityMiB(admitted.memory)}`}<//
                    >
                    <${Table.Td}>${`${current.cpu} → ${admitted.cpu}`}<//>
                  <//>`;
                })}
              <//>
            <//>
          <//>
          <${Text} size="sm" c="dimmed" mt="sm" mb=${0}>
            Results are a 09:21 UTC sample; limits matched the running pods in
            all dry runs. Initial does not evict running pods. Kyverno cleanup’s
            30-day peak was 123 MiB against its existing 128 MiB limit, so the
            request adjustment does not fix its narrow limit headroom.
          <//>
        <//>
        <${Paper} id="verified-rollouts" withBorder p="md" radius="md">
          <${Badge} color="green" variant="light"
            >3 · third cohort and follow-ups<//
          >
          <${Title} order=${3} size="h4" mt="xs">
            Crossplane, GitLab and control-plane workloads
          <//>
          <${Text} component="p">
            At ${latestTime} UTC the five Crossplane and four GitLab Deployments
            were Initial. External Secrets’ controller was Recreate with two
            ready replicas and a minimum of one available in its disruption
            budget. Its webhook stayed Initial. Kyverno admission remained
            Initial with three ready replicas and its disruption budget now
            requires two available.
          <//>
          <${Text} component="p">
            ${`All six PRs were merged at the observation. The VPA webhook ran for ${verifiedCount} of the ${latest.workloads.length} observed Deployments in server-side dry runs using actual ReplicaSet owners.`}
            ${" "}Limits and init-container resources were preserved. Dry runs
            cover admission only. Existing Pods, future scheduling, and
            updater-driven evictions are separate checks.
          <//>
          <${Text} component="p">
            Trust-manager, kubeapi-proxy, Home Assistant and the GitLab runner
            manager were all Initial and ready. The proxy, Home Assistant and
            runner needed namespace updates to pick up their Deployment
            annotations.${" "}
            ${`Trust-manager reported ${latest.trust_manager_bundles.synced} of ${latest.trust_manager_bundles.total} Bundles synced.`}
          <//>
          <${Text} component="p">
            ${latest.prs.map(
              (pr, index) =>
                html`<span key=${pr.number}>
                  ${index ? " · " : ""}<${Anchor} href=${pr.html_url}
                    >#${pr.number}<//
                  >
                </span>`,
            )}
          <//>
          <${Table.ScrollContainer}
            minWidth=${930}
            type="native"
            tabIndex=${0}
            aria-label="Third cohort and follow-up admission checks"
          >
            <${Table} striped withTableBorder verticalSpacing="sm">
              <${Table.Thead}>
                <${Table.Tr}>
                  <${Table.Th}>Namespace / Deployment<//>
                  <${Table.Th}>Ready<//>
                  <${Table.Th}>Live VPA<//>
                  <${Table.Th}>Memory request · running → dry run<//>
                  <${Table.Th}>CPU request · running → dry run<//>
                <//>
              <//>
              <${Table.Tbody}>
                ${latest.workloads.map((row) => {
                  const changes = (resource, format) =>
                    row.dry_run_admitted_resources
                      ? row.current_pod_resources.map((old, index) => {
                          const admitted =
                            row.dry_run_admitted_resources[index];
                          const label =
                            row.current_pod_resources.length > 1
                              ? `${old.name} · `
                              : "";
                          return html`<${Text}
                            component="div"
                            size="sm"
                            key=${old.name}
                          >
                            ${`${label}${format(old.requests[resource])} → ${format(admitted.requests[resource])}`}
                          <//>`;
                        })
                      : "Awaiting admission check";
                  return html`<${Table.Tr}
                    key=${`${row.namespace}/${row.deployment}`}
                  >
                    <${Table.Td}>${`${row.namespace} / ${row.deployment}`}<//>
                    <${Table.Td}
                      >${`${row.ready_replicas}/${row.desired_replicas}`}<//
                    >
                    <${Table.Td}>${modeLabels[row.vpa_mode] ?? row.vpa_mode}<//>
                    <${Table.Td}>${changes("memory", quantityMiB)}<//>
                    <${Table.Td}>${changes("cpu", (value) => value ?? "—")}<//>
                  <//>`;
                })}
              <//>
            <//>
          <//>
          <${Text} component="p" mt="sm">
            The GitLab cap model fit the three HPAs at their maxima and,
            separately, an individual rollout surge at current replica counts,
            with Crossplane on its observed placements. If two Crossplane
            providers moved to the two eligible Sidekiq workers at their 2 GiB
            caps, a 4 GiB Sidekiq Pod could become unschedulable. Placement and
            headroom can change. Rails, Workhorse and Toolbox stayed
            recommendation-only.
          <//>
          <${Text} component="p" mb="0">
            The singleton Redis operator stays recommendation-only. Reducing its
            500 MiB memory request below its limit would move it from Guaranteed
            to Burstable.
          <//>
        <//>
        <${Paper} id="rollout-follow-up" withBorder p="md" radius="md">
          <${Title} order=${3} size="h4">
            API proxy, GitLab Shell and MCP follow-up
          <//>
          <${Text} size="sm" c="dimmed" mt="xs">
            Observed 20 November 2026 at ${followupTime} UTC
          <//>
          <${Text} component="p" mt="sm">
            <strong>Kubernetes API proxy.</strong>${" "} Both replacements were
            Ready with two Ready Service endpoints and 10m CPU and 24 MiB memory
            requests. VPA stays Initial. The TCP readiness probe gates new
            endpoints, and the new disruption budget permits one disruption. An
            authenticated GET through the public proxy returned the Kubernetes
            version and the Ready Deployment. ${" "}<${Anchor}
              href=${followup.prs[0].html_url}
              >PR #18125<//
            >
          <//>
          <${Text} component="p" mt="sm">
            <strong>GitLab Shell.</strong>${" "} VPA Recreate evicted the two
            old Pods at 09:59:28 and 10:00:28 UTC. Both replacements were Ready
            on separate nodes with 23m CPU and about 100 and 121 MiB of memory
            requested, respectively. The Service had two Ready endpoints, and
            its disruption budget permitted one disruption. The SSH handshake
            passed with the pinned host key. An authenticated clone remains
            unverified because GitLab rejected the available client key. Two TCP
            samples showed zero connections. ${" "}<${Anchor}
              href=${followup.prs[1].html_url}
              >PR #18126<//
            >
          <//>
          <${Text} component="p" mt="sm" mb=${0}>
            <strong
              >Google MCP, HA MCP, SSH MCP, Plaid pgweb and Plaid Spend.</strong
            >${" "} All five Deployments were Ready with their original Pods.
            After namespace reconciliation, all five VPAs were Initial. Plaid
            web, sync and database provisioning remained recommendation-only.
            The webhook recognized the five selected containers in server dry
            runs and retained their existing requests and limits. At this
            observation none of the five VPAs had a recommendation, so request
            sizing was verified in the later checks below. ${" "}<${Anchor}
              href=${followup.prs[2].html_url}
              >PR #18127<//
            >
          <//>
        <//>
        <${Paper} id="rollout-wave" withBorder p="md" radius="md">
          <${Title} order=${3} size="h4">
            Initial memory requests and disruption protection
          <//>
          <${Text} size="sm" c="dimmed" mt="xs">
            Verified 20 November 2026 through ${waveTime} UTC
          <//>
          <${Text} component="p" mt="sm">
            <strong>MCP and Plaid admission.</strong>${" "} At 10:17 UTC, server
            dry runs with the live ReplicaSet owners applied 224 MiB to Google
            MCP, 256 MiB to the HA MCP backend, 144 MiB to SSH MCP, 100 MiB to
            Plaid pgweb and 384 MiB to Plaid Spend. CPU requests, all limits and
            HA MCP facade resources were preserved. Plaid web received no VPA
            mutation. ${" "}<${Anchor} href=${wave.prs[18127].html_url}
              >PR #18127<//
            >
          <//>
          <${Text} component="p" mt="sm">
            <strong>kubectl passthrough MCP.</strong>${" "} After a recorded
            cgroup OOM at the previous 256 MiB limit, the replacement was Ready
            at 10:21 UTC with a 192 MiB request and 384 MiB limit from its
            Deployment template. Every recorded rollout event had a Ready
            endpoint. VPA is Initial. ${" "}<${Anchor}
              href=${wave.prs[18134].html_url}
              >PR #18134<//
            >
          <//>
          <${Text} component="p" mt="sm">
            <strong>Platform Sessions.</strong>${" "} At 10:30 UTC, Initial
            admission applied about 156 MiB to Anthropic, 416 MiB to Blue
            Shield, 96 MiB to Gateway and 576 MiB to Quest Scheduler in server
            dry runs. The five other Deployments stayed recommendation-only. All
            nine existing Pod identities were preserved, and dry runs preserved
            CPU requests, limits and all init resources, including recorder
            sidecars. The 4 GiB GitLab Sidekiq surge fits at the observed OVH
            placements. Moving two capped Crossplane providers can consume that
            headroom even before these Platform Sessions changes.
            ${" "}<${Anchor} href=${wave.prs[18136].html_url}>PR #18136<//>
          <//>
          <${Text} component="p" mt="sm">
            <strong>CoreDNS and Cilium operator.</strong>${" "} Each budget had
            two healthy Pods and permitted one disruption. All four Pods kept
            their identities. Cluster DNS lookup succeeded and the Cilium
            operator lease was renewing. ${" "}<${Anchor}
              href=${wave.prs[18133].html_url}
              >PR #18133<//
            >
          <//>
          <${Text} component="p" mt="sm">
            <strong>Certificate admission.</strong>${" "} The cert-manager
            webhook had three Ready endpoints on three nodes, and trust-manager
            had two on two nodes. Both budgets permitted one disruption and both
            VPAs stayed Initial. Certificate and Bundle server dry runs passed
            and all six trust Bundles were synced. ${" "}<${Anchor}
              href=${wave.prs[18135].html_url}
              >PR #18135<//
            >
          <//>
          <${Text} component="p" mt="sm">
            <strong>External Secrets admission.</strong>${" "} The webhook had
            two Ready endpoints on separate nodes and its budget permitted one
            disruption. Its VPA stayed Initial and the two controller replicas
            stayed Ready in Recreate mode. The validating webhook accepted a
            server dry-run update and 198 of 198 ExternalSecrets were Ready.
            ${" "}<${Anchor} href=${wave.prs[18139].html_url}>PR #18139<//>
          <//>
          <${Text} component="p" mt="sm">
            <strong>KubeVirt operator.</strong>${" "} The new operator budget
            had two healthy Pods and permitted one disruption. Both unchanged
            Pods and their webhook endpoints were Ready on separate nodes. The
            operator lease was renewing and a KubeVirt server dry-run update
            passed. ${" "}<${Anchor} href=${wave.prs[18140].html_url}
              >PR #18140<//
            >
          <//>
          <${Text} component="p" mt="sm" mb=${0}>
            <strong>Kyverno webhooks.</strong>${" "} The cleanup webhook now has
            two Ready endpoints on two nodes and the admission webhook has three
            on separate control-plane nodes. Helm was Ready at 10:38 UTC. Both
            disruption budgets permit one disruption, and both VPAs stay
            Initial. ${" "}<${Anchor} href=${wave.prs[18142].html_url}
              >Cleanup HA<//
            >
            ${" · "}<${Anchor} href=${wave.prs[18143].html_url}
              >Admission placement<//
            >
          <//>
        <//>
        <${Paper} id="adoption-progress" withBorder p="md" radius="md">
          <${Title} order=${3} size="h4"
            >Kyverno, KEDA and VPA webhook follow-up<//
          >
          <${Text} size="sm" c="dimmed" mt="xs">
            Observed 20 November 2026 at ${progressTime} UTC
          <//>
          <${Text} component="p" mt="sm">
            <strong>Kyverno admission.</strong>${" "} VPA is Recreate. The three
            existing Pods stayed Ready on separate control-plane nodes through
            the mode change, with three Ready endpoints and one disruption
            allowed. ${" "}<${Anchor} href=${progress.prs[18147].html_url}
              >PR #18147<//
            >
          <//>
          <${Text} component="p" mt="sm">
            <strong>KEDA admission.</strong>${" "} Two Ready webhook endpoints
            serve from separate nodes, with one disruption allowed. Direct TLS
            checks verified both endpoints, and an invalid ScaledObject dry run
            was denied. VPA stays Initial. ${" "}<${Anchor}
              href=${progress.prs[18148].html_url}
              >PR #18148<//
            >
          <//>
          <${Text} component="p" mt="sm">
            <strong>KEDA external metrics.</strong>${" "} Two Ready endpoints
            serve from separate nodes, with one disruption allowed. Both passed
            TLS readiness with the APIService’s CA and DNS identity, and a live
            autoscaler metric query succeeded. VPA stays Initial.
            ${" "}<${Anchor} href=${progress.prs[18152].html_url}>PR #18152<//>
          <//>
          <${Text} component="p" mt="sm">
            <strong>VPA webhook.</strong>${" "} The two replacements were Ready
            on separate nodes, each passed direct TLS verification, and the
            budget allowed one disruption. ${" "}<${Anchor}
              href=${progress.prs[18149].html_url}
              >PR #18149<//
            >
          <//>
          <${Text} component="p" mt="sm" mb=${0}>
            <strong>VPA owner-cache race.</strong>${" "} At 10:56:16 UTC the old
            webhook could not find the first new Pod’s ReplicaSet in its
            informer cache and reported no matching VPA. The Pod retained its
            declared 25m CPU and 128 MiB memory requests. At 10:56:27 UTC the
            same webhook applied 15m CPU and about 105 MiB memory to the second
            Pod. API server fail-open counters stayed at zero. Check actual
            requests on the first Pods from a new ReplicaSet, even when an
            existing owner passed admission dry runs. Upstream investigation
            remains open on how admission should handle an owner absent from the
            cache. ${" "}<${Anchor}
              href=${progress.rollouts["vpa-admission-controller"].owner_cache_race.source}
              >VPA owner lookup<//
            >
            ${" · "}<${Anchor}
              href=${progress.rollouts["vpa-admission-controller"].owner_cache_race.matcher_source}
              >Admission matcher<//
            >
          <//>
        <//>
        <${Paper} id="cert-manager-ha" withBorder p="md" radius="md">
          <${Title} order=${3} size="h4"
            >cert-manager controller and CA injector<//
          >
          <${Text} size="sm" c="dimmed" mt="xs">
            Observed 20 November 2026 at ${certManagerTime} UTC
          <//>
          <${Text} component="p" mt="sm" mb=${0}>
            Both Deployments had two Ready replicas on separate hosts. Their
            disruption budgets each permitted one disruption and both VPAs
            stayed Initial. Observed leader lease transition gaps were about 6.8
            seconds and 5.5 seconds. All 59 Certificates were Ready. The three
            existing webhook endpoints and six CA digests were unchanged, and
            Certificate and Bundle server dry runs passed. ${" "}<${Anchor}
              href=${certManagerHA.html_url}
              >PR #18177<//
            >
          <//>
        <//>
        <${Paper} id="controller-ha" withBorder p="md" radius="md">
          <${Title} order=${3} size="h4">Crossplane and resource metrics API<//>
          <${Text} size="sm" c="dimmed" mt="xs">
            Observed 20 November 2026 at ${controllerHATime} UTC
          <//>
          <${Text} component="p" mt="sm">
            <strong>Crossplane core.</strong>${" "}Flux and Helm reported Ready.
            Two Ready webhook endpoints served from${" "}
            ${crossplaneHA.ready_endpoints.map(({ node }) => node).join(" and ")}.
            ${` The disruption budget allowed ${crossplaneHA.disruptions_allowed} disruption, and the core leader lease was renewing. Both Pods received a ${quantityMiB(crossplaneHA.admitted_memory_request)} memory request with a ${quantityMiB(crossplaneHA.admitted_memory_limit)} limit.`}${" "}<${Anchor}
              href=${crossplaneHA.pr_url}
              >PR #18145<//
            >
          <//>
          <${Text} component="p" mt="sm">
            <strong>metrics-server.</strong>${" "}Flux and Helm reported Ready.
            Two Ready endpoints served from${" "}
            ${metricsHA.ready_endpoints.map(({ node }) => node).join(" and ")}.
            The disruption budget allowed ${metricsHA.disruptions_allowed}${" "}
            disruption and the aggregated APIService was Available. It returned
            metrics for ${metricsHA.node_metrics_returned} nodes at${" "}
            ${controllerHATime} UTC and${" "}
            ${metricsHA.earlier_node_metrics_returned} in an adjacent sample.
            Four successive checks at 17:58 UTC returned metrics for all nine
            Ready nodes. Both Pods received a${" "}
            ${quantityMiB(metricsHA.admitted_memory_request)} memory request
            with a ${quantityMiB(metricsHA.admitted_memory_limit)} limit.
            Hostname anti-affinity preserves host separation, while zone
            placement can change.${" "}<${Anchor} href=${metricsHA.pr_url}
              >PR #18154<//
            >
          <//>
          <${Text} component="p" mt="sm">
            At 20:21 UTC, after ${" "}<${Anchor}
              href=${crossplaneSiteSpread.pr_url}
              >PR #18253<//
            >, Crossplane had three Ready webhook endpoints on optiplex, wyrm2
            and ovh-ns103711 in three zones. Its budget required two replicas
            and allowed one disruption; the core leader lease was renewing. Each
            Pod requested
            ${" "}${quantityMiB(crossplaneSiteSpread.memory_request)} ${" "}of
            memory with a 1 GiB limit. Sidekiq was 1/1 available. This
            observation is not a fault-injection test or a durable capacity
            guarantee.
          <//>
          <${Text} component="p" mt="sm" mb=${0}>
            <strong>Sidekiq capacity.</strong>${" "}Sidekiq was${" "}
            ${sidekiq.available_replicas}/${sidekiq.desired_replicas} available.
            The 17:44 UTC placement model left about${" "}
            ${sidekiq.adverse_scenario_remaining_mib_on_ovh_ns103711.toLocaleString("en-US")}${" "}
            MiB on ovh-ns103711 in a modeled overlap involving a provider move,
            controller replacements and ${" "}<${Anchor}
              href=${sidekiq.staging_proposal_url}
              >staging rollout<//
            >
            ${" "}at proposed caps. That falls below its 3 GiB recommendation
            and 4 GiB cap. An ordinary blocked surge leaves the incumbent in
            place, while a concurrent host loss could leave Sidekiq unavailable.
            Check current OVH headroom before overlapping rollouts. The
            metrics-server VPA still has no explicit request cap.
          <//>
        <//>
        <${Paper} id="agentplane-staging-initial" withBorder p="md" radius="md">
          <${Title} order=${3} size="h4">
            Agentplane staging service requests
          <//>
          <${Text} size="sm" c="dimmed" mt="xs">
            Observed 20 November 2026 at ${stagingInitialTime} UTC
          <//>
          <${Text} component="p" mt="sm">
            Flux applied ${stagingInitial.targets.length} explicit Initial VPAs
            for ${stagingInitial.targets.join(", ")}. They manage memory
            requests only. The namespace remains opted out of Goldilocks.
            ${" "}<${Anchor} href=${stagingInitial.pr_url}>PR #18195<//>
          <//>
          <${Text} component="p" mt="sm">
            The ${stagingInitial.ready_pods} existing service Pods were Ready
            and older than the merge. Recommendations appeared at 18:00 UTC.
            Server-side Pod CREATE dry runs at ${stagingAdmissionTime} UTC,
            using the existing ReplicaSet owners, admitted 40 MiB for frontend,
            256 MiB for sessions and 128 MiB for each other selected container.
            All eight kept their CPU requests and limits. The dry runs created
            no Pods. Verify resources and scheduling on actual replacements.
          <//>
          <${Text} component="p" mt="sm" mb=${0}>
            <${Anchor} href=${stagingControllers.pr_url}>PR #18245<//>
            ${" "}added environments and policy-controller to Initial
            memory-request sizing. Both Deployments remained 2/2 Ready, and both
            recommend 100 MiB. ReplicaSet-owner Pod CREATE dry runs admitted 10m
            CPU and 100 MiB memory with limits preserved. The four existing Pods
            predate the change; natural admission and scheduling remain
            unverified.
          <//>
        <//>
        <${Paper} id="web-frontend-ha" withBorder p="md" radius="md">
          <${Title} order=${3} size="h4"
            >Website and Haku Console static replicas<//
          >
          <${Text} size="sm" c="dimmed" mt="xs">
            Observed 20 November 2026 at ${webFrontendHATime} UTC
          <//>
          <${Text} component="p" mt="sm">
            Website and Haku Console static each had two Ready endpoints on
            ${" "}${webFrontendHA.website.nodes.join(" and ")}, with one allowed
            disruption per budget. Both website Pods returned HTTP 200 for / and
            /health; both Haku static Pods returned 200 for /. The website Pods
            each requested ${" "}${webFrontendHA.website.request_mib} MiB and
            Haku static ${" "}${webFrontendHA.haku_static.request_mib} MiB. Both
            VPAs remain in Recreate mode and manage requests only.
            ${" "}<${Anchor} href=${webFrontendHA.website.pr_url}
              >Website PR #18210<//
            >${" · "}<${Anchor} href=${webFrontendHA.haku_static.pr_url}
              >Haku PR #18211<//
            >
          <//>
          <${Text} component="p" mt="sm" mb=${0}>
            Both use revision-scoped hostname spread with two minimum domains.
            If only one host is eligible, the second replica remains Pending;
            hostname separation does not guarantee zone separation. Anonymous
            requests to both public entry routes returned HTTP 200.
            Authenticated Haku actions and recovery after an actual host failure
            were not tested.
          <//>
        <//>
        <${Paper}
          id="clickhouse-exporter-initial"
          withBorder
          p="md"
          radius="md"
        >
          <${Title} order=${3} size="h4"
            >ClickHouse operator exporter requests<//
          >
          <${Text} size="sm" c="dimmed" mt="xs">
            Observed 20 November 2026 at ${clickhouseExporterTime} UTC
          <//>
          <${Text} component="p" mt="sm" mb=${0}>
            Flux and Helm were Ready after applying an Initial VPA policy for
            the operator Deployment's metrics-exporter memory request. Its
            recommendation is bounded to 96–128 MiB. The operator container
            stays excluded, with its prior 50m CPU and 128 MiB memory requests
            pinned to guard against stale recommendations. VPA status still
            contained an older operator recommendation. A server-side Pod CREATE
            dry run using the actual ReplicaSet owner admitted those operator
            requests and a 96 MiB exporter request, preserving both containers'
            CPU requests and limits. The existing operator Pod remained 2/2
            Ready and predates the change; actual replacement and scheduling
            remain to be observed. ClickHouse and Keeper StatefulSet VPAs remain
            Off. ${" "}<${Anchor} href=${clickhouseExporter.pr_url}
              >PR #18214<//
            >
          <//>
        <//>
      <//>
      <${Title} id="remaining-work" order=${3} mt="xl">Remaining work<//>
      <${Stack} gap="sm" mt="sm">
        <${Paper} withBorder p="md" radius="md">
          <${Badge} color="blue" variant="light"
            >Initial coverage · in progress<//
          >
          <${Title} order=${3} size="h4" mt="xs"
            >Audit the next eligible workload cohorts<//
          >
          <${Text} component="p" mb=${0}>
            Reconcile the remaining recommendation-only and uncovered
            controllers against the deployed cohorts above. The 02:58 snapshot
            had 89 Off VPA objects and 68 of 237 controller templates without a
            direct target. These overlapping inventories count different
            resource kinds. At 17:40 UTC, a fresh cluster inventory found 215
            VPAs: 94 Initial, 67 Off and 54 Recreate. The older chart and
            searchable inventory above retain their original snapshot. All 52
            controller templates without a direct VPA in the fresh inventory
            were in namespaces explicitly opted out of Goldilocks; assess their
            owners before opting in. Prioritize supported Deployments with
            credible startup history and request headroom. Preserve existing
            requests-and-limits policies and CPU-utilization HPA ownership.
            GitLab webservice stays excluded pending surge capacity, Redis
            operator stays Off to preserve its QoS class, and the five
            unselected Platform Sessions Deployments stay Off.
          <//>
        <//>
        <${Paper} withBorder p="md" radius="md">
          <${Badge} color="cyan" variant="light">Automatic cohorts<//>
          <${Title} order=${3} size="h4" mt="xs"
            >Promote replicated workloads to Recreate in groups<//
          >
          <${Text} component="p" mb=${0}>
            GitLab Shell, the External Secrets controller and Kyverno admission
            are in Recreate. The proxy, CoreDNS, Cilium operator and the
            certificate, External Secrets and KEDA webhooks have disruption
            budgets and remain Initial. Evaluate eviction for each dependency
            independently, including placement, endpoint readiness, client
            behavior and failure recovery. The ${" "}<${Anchor}
              href="#controller-ha"
              >Crossplane and metrics-server follow-up<//
            >
            ${" "}records their deployed replicas and the remaining Sidekiq
            capacity risk. CDviz and ntfy stay non-evicting pending delivery and
            availability proof. Review every VPA in a namespace before lowering
            its minimum replica count.
          <//>
        <//>
        <${Paper} withBorder p="md" radius="md">
          <${Badge} color="gray" variant="light">Operator-owned exceptions<//>
          <${Title} order=${3} size="h4" mt="xs"
            >Cover databases and dynamic workloads through their owners<//
          >
          <${Text} component="p" mb=${0}>
            Start with one explicitly owned CNPG recommendation pilot and check
            reconciliation and admission against the operator’s templates.
            Coordinate CSI and other storage sizing with the storage plan.
            Decide on explicit owner resources for operator-created sandboxes,
            Terraform runners, VMs and short-lived Jobs where transient peaks or
            admission paths preclude a shared VPA.
          <//>
        <//>
      <//>
      <${Text} component="p" mt="md">
        VPA admission can miss a newly created Pod while its ReplicaSet is
        absent from the webhook's informer cache. Keep safe template requests,
        verify the first replacement after rollout, and continue VPA adoption.
        The upstream
        <${Anchor} href="https://github.com/kubernetes/autoscaler/issues/10564"
          >informer-lag issue<//
        >
        and
        <${Anchor} href="https://github.com/kubernetes/autoscaler/pull/10625"
          >proposed fallback fix<//
        >
        track the issue.
      <//>
      <${Text} component="p" mt="md">
        VPA 1.7.1 can briefly admit a stale CPU recommendation after a policy
        switches from Off to memory-only Initial. The first cohort pins existing
        CPU requests with equal minimum and maximum bounds through that
        transition; the webhook with no prior CPU request explicitly adopts CPU
        sizing instead. Verify live target resources and limits, not just the
        requested mode. ${" "}<${Anchor}
          href="https://github.com/kubernetes/autoscaler/blob/vertical-pod-autoscaler-1.7.1/vertical-pod-autoscaler/pkg/utils/vpa/capping.go"
          >VPA admission capping<//
        >
      <//>
      <${Text} component="p" mt="md">
        For each cohort, compare generated policies and existing request/limit
        ownership, HPA and PDB interactions, realistic recommendations and
        startup bounds, and total scheduling impact in eligible zones. Verify
        each live VPA after reconciliation, then verify admitted resources,
        scheduling and health as pods are normally created. Track cohort-level
        coverage and failures; stop or revert a cohort if its gates fail.
      <//>
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
        constraints, not today’s usage. The original snapshot collection was
        read-only; the later CDviz namespace update was an operational
        intervention, separately described above.<//
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
