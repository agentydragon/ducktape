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
    "Compare chart and operator output, API defaulting and admitted values; preserve rollback history and cleanup behavior.",
  "default-disable-service-links":
    "Account for every operator-created Pod, not only cdk8s templates; test explicit and absent fields.",
  "require-gitops":
    "It is audit-only today. Decide whether to retain its signals and verify subject and operator exemptions.",
  "restrict-agent-gateway-routes":
    "Preserve enforced namespace, route-kind and API-version coverage, including direct API writes.",
  "require-secret-store-conditions":
    "Preserve enforced admission for private and public sources and separately replace the existing-object background scan.",
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
    fetch("./evidence.json?v=batches-20261120-0824")
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
        resource ownership by workload class. Roll out shared policies in
        cohorts, preserving existing request-and-limit policies unless evidence
        justifies changing them.<//
      >
      <${Text} size="sm" c="dimmed" mt="sm"
        >${`Cluster snapshot around 20 November 2026, 02:58 UTC · latest seven-day query ${data.history_end_utc} · inspected devel ${data.source_devel_commit}. Batch review and selected live checks: 20 November 2026, 08:24 UTC. Inventory and charts remain historical.`}<//
      >
      <${Group} mt="sm" gap="md"
        ><${Anchor} href="./evidence.json">Evidence and inventory JSON<//
        ><${Anchor} href="./manifest.json">Manifest<//
        ><${Anchor} href="#architecture">Policy refactor<//
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
        >Define VPA behavior once; retire Kyverno incrementally<//
      >
      <${Text} size="sm" c="dimmed">
        ${`Architecture review: ${data.architecture_review.observed_at_utc}; devel ${data.architecture_review.source_devel_commit}. The workload and metrics snapshot above remains from 02:58 UTC.`}
      <//>
      <${Text} component="p">
        The intended direction is broader VPA request coverage and, eventually,
        no Kyverno. Author the common behavior in cdk8s once, keep Goldilocks as
        the owner of VPAs it generates, and remove each Kyverno responsibility
        only after its remaining runtime inputs have another owner. The shared
        profile refactor and Kyverno VPA default removal are merged; adoption
        can proceed in coherent, independently reviewable cohorts.
      <//>
      <${Title} order=${3} size="h4" mt="md"
        >Small profiles, explicit exceptions<//
      >
      <${Text} component="p">
        The merged <${Code}>VpaProfile<//> and <${Code}>ContainerPolicy<//>
        helpers express modes, request/limit ownership, bounds and exclusions.
        Apply shared profiles at the namespace level where every workload is
        eligible; use workload-level overrides or split namespaces where they
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
                >Shared typed profile and annotation builders merged in
                #18021.<//
              ><${Table.Td}
                >Compare rendered metadata and effective VPA specs; preserve
                modes, values, overrides and single ownership.<//
              ><//
            >
            <${Table.Tr}
              ><${Table.Td}>2 · Adopt<//
              ><${Table.Td}
                >Apply cohort-wide Initial profiles, then enable Recreate for
                suitable replicated workloads in batches.<//
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
              ><${Table.Td}>4 · Remove Kyverno<//
              ><${Table.Td}
                >Migrate its remaining defaults, security enforcement, dynamic
                injection and cleanup separately.<//
              ><${Table.Td}
                >Prove each replacement on real admission/cleanup paths,
                including private sources and failure behavior, before disabling
                the policy or controller.<//
              ><//
            >
          <//>
        <//>
      <//>
      <${Title} order=${3} size="h4" mt="lg">What Kyverno still owns<//>
      <${Text} component="p">
        The 03:36 UTC inventory found 7 ClusterPolicies and 2 namespaced
        CleanupPolicies. The Goldilocks default was subsequently removed; six
        ClusterPolicies and both cleanup policies remained at 08:24 UTC. This is
        not an inventory of every test cluster or private repository. cdk8s can
        render its own inputs, but cannot retroactively author Pods created by
        KubeVirt or another operator.
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
              ><${Table.Th}>Live policy<//
              ><${Table.Th}>Current responsibility<//
              ><${Table.Th}>Candidate direction<//
              ><${Table.Th}>Before removal<//><//
            ><//
          >
          <${Table.Tbody}>
            ${data.architecture_review.kyverno.policies
              .filter((p) => p.name !== "default-vpa-requests-only")
              .map(
                (p) =>
                  html`<${Table.Tr} key=${p.name}>
                    <${Table.Td}><${Code}>${p.name}<//><//
                    ><${Table.Td}>${p.role}<//><${Table.Td}>${p.direction}<//
                    ><${Table.Td}>${cutoverProof[p.name]}<//>
                  <//>`,
              )}
            <${Table.Tr}>
              <${Table.Td}
                ><${Code}>sandbox-janitor<//> / <${Code}>reap-wedged-jobs<//><//
              >
              <${Table.Td}
                >Scheduled cleanup in claude-sandbox and thrive-scraper.<//
              >
              <${Table.Td}
                >Use an appropriately scoped janitor or native TTL where it
                actually applies.<//
              >
              <${Table.Td}
                >Preserve age, selection, permissions and schedule; Job TTL only
                handles finished Jobs, not wedged active Jobs or other resource
                kinds.<//
              >
            <//>
          <//>
        <//>
      <//>
      <${Text} component="p">
        The existing revision-history policy intends to default to 3, but 224 of
        237 live long-running controllers had 10 at this later observation. That
        count does not tell us whether charts, API defaulting or policy matching
        supplied each value; investigate before assuming the policy can simply
        be deleted.
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
      <${Title} order=${2} mt="xl">Recommended batches<//>
      <${Text} component="p">
        The goal is requests for almost all supported workloads, with automatic
        resizing wherever disruption is acceptable. Do not serialize the rollout
        one Deployment at a time: select cohorts by ownership and risk, use the
        shared profile once per namespace or chart, and review the effective
        policies together. The examples below were checked around 08:24 UTC;
        validate current resource values and eligibility before changing them.
      <//>
      <${Alert} color="orange" title="Reconciliation is a rollout gate" my="md">
        Goldilocks watches Pods and Namespaces, not Deployment metadata. CDviz’s
        annotations landed after its last reconciliation and its VPA stayed Off
        until a namespace update at 08:17 UTC. Verify every intended VPA after
        each batch and trigger a namespace update when needed; investigate a
        durable level-triggered fix. CDviz is now Initial, memory-only and
        RequestsOnly; its existing pod remains running and new-pod admission has
        not yet been verified. ${" "}
        <${Anchor}
          href="https://github.com/FairwindsOps/goldilocks/blob/v4.16.1/pkg/controller/controller.go"
        >
          Goldilocks watchers<//
        >
      <//>
      <${Stack} gap="sm">
        <${Paper} withBorder p="md" radius="md">
          <${Badge} color="blue" variant="light">1 · broad Initial cohorts<//>
          <${Title} order=${3} size="h4" mt="xs"
            >Adopt requests together across ordinary controllers<//
          >
          <${Text} component="p">
            First proposed cohort: external-dns; cert-manager controller,
            cainjector and webhook; external-secrets controller and webhook; and
            the three KEDA Deployments. These nine currently have Off VPAs with
            recommendations. Group changes by chart or namespace in one or a few
            PRs and stage dependent infrastructure families separately; they do
            not need nine serial pilots. Use Initial and RequestsOnly for new
            policies, initially controlling memory where CPU ownership or HPA is
            uncertain. Existing CPU requests, limits and policies stay intact.
          <//>
          <${Text} component="p" mb=${0}>
            These are candidates, not pre-approved changes: confirm the chart’s
            metadata support, full namespace membership, recommendation quality,
            startup peaks, minimums, limits and aggregate node/zone headroom.
            Constrain low recommendations where appropriate. Initial will not
            evict existing pods, but a later ordinary replacement will receive a
            different request and could fail to schedule; it does not repair an
            under-requested running pod. Trust-manager is a later candidate
            after assessing its fail-closed webhook and startup behavior.
          <//>
        <//>
        <${Paper} withBorder p="md" radius="md">
          <${Badge} color="blue" variant="light">2 · coverage by class<//>
          <${Title} order=${3} size="h4" mt="xs"
            >Expand to eligible namespaces, not a bespoke change per workload<//
          >
          <${Text} component="p" mb=${0}>
            Inventory all recommendation-only and unprotected controllers; apply
            shared Initial request profiles in cohorts where every affected
            workload is understood. The 02:58 snapshot had 89 Off VPA objects
            and 68 of 237 controller templates without a direct VPA target;
            these populations overlap imperfectly and include different resource
            kinds. Separate existing requests-and-limits VPAs, HPAs, singleton
            delivery receivers, unsupported target owners and disabled
            namespaces into explicit exceptions. Add recommendations first for
            eligible uncovered controllers; do not treat lack of a VPA as proof
            that namespace mode alone will cover a custom operator target.
          <//>
        <//>
        <${Paper} withBorder p="md" radius="md">
          <${Badge} color="cyan" variant="light">3 · automatic cohorts<//>
          <${Title} order=${3} size="h4" mt="xs"
            >Promote replicated workloads to Recreate in groups<//
          >
          <${Text} component="p" mb=${0}>
            Candidate replicated controllers include external-secrets,
            kubeapi-proxy and the Kyverno admission controller. Check each
            cohort’s rollout topology, PDB, redundancy, availability budget,
            startup and node fit before enabling eviction; avoid simultaneous
            changes across tightly coupled control-plane dependencies. VPA’s
            installed two-replica minimum is a guard, not a guarantee of service
            availability. Preserve existing request-and-limit ownership. Do not
            lower namespace-wide <${Code}>minReplicas<//> for a singleton
            without reviewing every VPA in that namespace. CDviz and ntfy remain
            non-evicting until their delivery and availability requirements are
            addressed. For CPU HPA targets, keep CPU ownership separate or prove
            the interaction; memory-only policies are a useful default.
          <//>
        <//>
        <${Paper} withBorder p="md" radius="md">
          <${Badge} color="gray" variant="light"
            >4 · operator-owned exceptions<//
          >
          <${Title} order=${3} size="h4" mt="xs"
            >Cover databases and dynamic workloads through their owners<//
          >
          <${Text} component="p" mb=${0}>
            Group CNPG, CSI and other operator-managed workloads by owner. A
            Goldilocks namespace switch does not prove custom targets are
            covered; establish one VPA owner and prove admission and operator
            reconciliation, or set explicit requests in the owning resource. For
            sandboxes, Terraform runners, VMs and short-lived Jobs, account for
            transient peaks and admission paths before broadening coverage.
            Coordinate storage workloads with the separate storage work.
          <//>
        <//>
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
