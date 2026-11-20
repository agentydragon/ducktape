import { Code, Paper, Stack, Text } from "@mantine/core";
import type { JSX, ReactNode } from "react";
import { parseDocument } from "yaml";
import { z } from "zod";

import { kubernetesTarget } from "../../schemas/kubernetes_admin/target";
import { definePreview, type ArgumentsPreview } from "../entry";
import { type ActionPresentation, expandsForRejectedArguments } from "../presentation";
import { defineResultPreview } from "../result_entry";
import { parseKubernetesTable, resourceListResult } from "./list_result";
import { resource } from "./resource_common";
import { kubernetesResourcesResult, kubernetesResourcesSchema } from "./resources";
import { kubernetesTextResult } from "./text_result";

const string = z.string().min(1);
const empty = z.strictObject({});
const podSelectors = z.strictObject({ labelSelector: string.optional(), fieldSelector: string.optional() });
const namespaces = z.strictObject({ fieldSelector: string.optional() });
const node = z.strictObject({ name: string });
const nodeLogs = z.strictObject({ name: string, query: string, tailLines: z.int().nonnegative().optional() });
const nodesTop = z.strictObject({ name: string.optional(), label_selector: string.optional() });
const podsTop = nodesTop.extend({ namespace: string.optional(), all_namespaces: z.boolean().optional() });
const configuration = z.strictObject({ minified: z.boolean().optional() });
const podRun = z.strictObject({
  name: string.optional(),
  namespace: string.optional(),
  image: string,
  port: z.int().optional(),
});
const scale = z.strictObject({ ...resource, scale: z.int().optional() });

function selectors(label?: string, field?: string): JSX.Element | null {
  if (!label && !field) return null;
  return (
    <Text size="sm" style={{ overflowWrap: "anywhere" }}>
      {label && (
        <>
          Label selector <Code>{label}</Code>
        </>
      )}
      {label && field && " · "}
      {field && (
        <>
          Field selector <Code>{field}</Code>
        </>
      )}
    </Text>
  );
}

const allPodsFilters = definePreview(podSelectors, ({ args }) => selectors(args.labelSelector, args.fieldSelector));
const namespaceFilters = definePreview(namespaces, ({ args }) => selectors(undefined, args.fieldSelector));

function Label({ children }: { children: ReactNode }): JSX.Element {
  return (
    <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
      {children}
    </Text>
  );
}

const nodeLogLabel = definePreview(nodeLogs, ({ args }) => (
  <Label>
    View logs for Node <Code>{args.name}</Code>
  </Label>
));
const nodeLogArguments = definePreview(nodeLogs, ({ args }) => (
  <Text size="sm" style={{ overflowWrap: "anywhere" }}>
    Source <Code>{args.query}</Code>
    {args.tailLines && args.tailLines > 0
      ? ` · Last ${args.tailLines} ${args.tailLines === 1 ? "line" : "lines"}`
      : " · No line limit requested"}
  </Text>
));

const nodeStatsLabel = definePreview(node, ({ args }) => (
  <Label>
    Read resource usage for Node <Code>{args.name}</Code>
  </Label>
));
const statsSchema = z.object({
  node: z.object({
    nodeName: string,
    startTime: z.string().optional(),
    cpu: z.object({ time: z.string().optional(), usageNanoCores: z.number().nonnegative().optional() }).optional(),
    memory: z
      .object({
        time: z.string().optional(),
        usageBytes: z.number().nonnegative().optional(),
        workingSetBytes: z.number().nonnegative().optional(),
      })
      .optional(),
  }),
});
const metricAmount = new Intl.NumberFormat("en-US", { maximumSignificantDigits: 4 });
const nodeStatsResult = defineResultPreview(statsSchema, ({ result: { node: value } }) => (
  <Stack gap={4}>
    <Text size="sm" fw={600}>
      {value.nodeName}
    </Text>
    {value.cpu?.usageNanoCores !== undefined && (
      <Text size="sm">
        CPU · {metricAmount.format(value.cpu.usageNanoCores / 1e9)} cores{value.cpu.time && ` · ${value.cpu.time}`}
      </Text>
    )}
    {value.memory?.usageBytes !== undefined && (
      <Text size="sm">
        Memory · {metricAmount.format(value.memory.usageBytes / 2 ** 20)} MiB used
        {value.memory.time && ` · ${value.memory.time}`}
      </Text>
    )}
    {value.memory?.workingSetBytes !== undefined && (
      <Text size="sm">Working set · {metricAmount.format(value.memory.workingSetBytes / 2 ** 20)} MiB</Text>
    )}
    {value.cpu?.usageNanoCores === undefined &&
      value.memory?.usageBytes === undefined &&
      value.memory?.workingSetBytes === undefined && (
        <Text size="sm" c="dimmed">
          This sample did not include CPU or memory usage.
        </Text>
      )}
  </Stack>
));

const nodesTopLabel = definePreview(nodesTop, ({ args }) =>
  args.name ? (
    <Label>
      Read resource usage for Node <Code>{args.name}</Code>
    </Label>
  ) : (
    "Read resource usage across nodes"
  )
);
const nodesTopFilters = definePreview(nodesTop, ({ args }) =>
  args.name && args.label_selector ? (
    <Text size="sm">
      Capacity lookup selector <Code>{args.label_selector}</Code>
    </Text>
  ) : (
    selectors(args.label_selector)
  )
);
const podsTopLabel = definePreview(podsTop, ({ args }) => {
  const scope = args.namespace ? (
    <>
      namespace <Code>{args.namespace}</Code>
    </>
  ) : args.all_namespaces === false ? (
    "the current namespace"
  ) : (
    "all namespaces"
  );
  return (
    <Label>
      Read resource usage for{" "}
      {args.name ? (
        <>
          Pod <Code>{args.name}</Code>
        </>
      ) : (
        "Pods"
      )}
      {" in "}
      {scope}
    </Label>
  );
});
const podsTopFilters = definePreview(podsTop, ({ args }) =>
  args.name && args.label_selector ? (
    <Text size="sm">The label selector does not apply to a named Pod.</Text>
  ) : (
    selectors(args.label_selector)
  )
);

const metricsTable = z.string().transform((text, context) => {
  const rows = parseKubernetesTable(text);
  if (rows && rows.every((row) => "CPU(cores)" in row || "MEMORY(bytes)" in row)) return rows;
  context.addIssue({ code: "custom", message: "Expected a Kubernetes metrics table" });
  return z.NEVER;
});
const metricsResult = defineResultPreview(metricsTable, ({ result }) => (
  <Stack gap="xs">
    {result.length === 0 && (
      <Text size="sm" c="dimmed">
        No metrics were returned.
      </Text>
    )}
    {result.map((row) => (
      <Paper key={`${row.NAMESPACE ?? ""}/${row.NAME}`} withBorder p="xs">
        <Text size="sm" fw={600}>
          <Code>{row.NAMESPACE ? `${row.NAMESPACE}/${row.NAME}` : row.NAME}</Code>
        </Text>
        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
          {[
            row["CPU(cores)"] && `CPU ${row["CPU(cores)"]}${row["CPU(%)"] ? ` (${row["CPU(%)"]})` : ""}`,
            row["MEMORY(bytes)"] && `Memory ${row["MEMORY(bytes)"]}${row["MEMORY(%)"] ? ` (${row["MEMORY(%)"]})` : ""}`,
            row["SWAP(bytes)"] && `Swap ${row["SWAP(bytes)"]}${row["SWAP(%)"] ? ` (${row["SWAP(%)"]})` : ""}`,
          ]
            .filter(Boolean)
            .join(" · ")}
        </Text>
      </Paper>
    ))}
  </Stack>
));

const configLabel = definePreview(configuration, ({ args }) =>
  args.minified === false
    ? "Read configuration for all Kubernetes contexts"
    : "Read configuration for the current Kubernetes context"
);
const configSummary = z.object({
  apiVersion: z.literal("v1"),
  kind: z.literal("Config"),
  "current-context": z.string().optional(),
  contexts: z
    .array(
      z.object({
        name: z.string(),
        context: z.object({ cluster: z.string().optional(), namespace: z.string().optional() }),
      })
    )
    .nullish(),
});

const configResult = defineResultPreview(z.string(), ({ result }) => {
  const document = parseDocument(result, { prettyErrors: false, uniqueKeys: true });
  const config = (() => {
    try {
      return document.errors.length ? undefined : configSummary.safeParse(document.toJS()).data;
    } catch {
      return undefined;
    }
  })();
  if (config === undefined) return <Text size="sm">Open Raw to inspect the recorded configuration.</Text>;
  const current = config.contexts?.find(({ name }) => name === config["current-context"]);
  return (
    <Stack gap={4}>
      {config["current-context"] && (
        <Text size="sm">
          Current context · <Code>{config["current-context"]}</Code>
        </Text>
      )}
      {current?.context.cluster && (
        <Text size="sm">
          Cluster · <Code>{current.context.cluster}</Code>
        </Text>
      )}
      {current?.context.namespace && (
        <Text size="sm">
          Namespace · <Code>{current.context.namespace}</Code>
        </Text>
      )}
      {config.contexts && config.contexts.length > 1 && (
        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
          Configured contexts · {config.contexts.map(({ name }) => name).join(", ")}
        </Text>
      )}
      {!config["current-context"] && (
        <Text size="sm" c="dimmed">
          No current context was reported.
        </Text>
      )}
    </Stack>
  );
});

const runLabel = definePreview(podRun, ({ args }) => (
  <Label>
    Run {args.name ? <Code>{kubernetesTarget("Pod", args.name, args.namespace)}</Code> : "a Pod with a generated name"}
  </Label>
));
const runArguments = definePreview(podRun, ({ args }) => (
  <Stack gap={4}>
    <Text size="sm" style={{ overflowWrap: "anywhere" }}>
      Image · <Code>{args.image}</Code>
    </Text>
    {!args.namespace && <Text size="sm">Use the current namespace.</Text>}
    {!args.name && args.namespace && (
      <Text size="sm">
        Namespace · <Code>{args.namespace}</Code>
      </Text>
    )}
    {args.port && args.port > 0 ? (
      <Text size="sm">
        Expose TCP port {args.port} through a ClusterIP Service. May also create an OpenShift Route where supported.
      </Text>
    ) : (
      <Text size="sm">No Service port requested.</Text>
    )}
  </Stack>
));
const scaleLabel = definePreview(scale, ({ args }) => (
  <Label>
    {args.scale === undefined ? "Read scale for" : "Set desired replicas for"}{" "}
    <Code>{kubernetesTarget(args.kind, args.name, args.namespace)}</Code>
  </Label>
));
const scaleArguments = definePreview(scale, ({ args }) => (
  <Stack gap={4}>
    <Text size="sm">
      API version · <Code>{args.apiVersion}</Code>
    </Text>
    {args.scale !== undefined && <Text size="sm">Desired replicas · {args.scale}</Text>}
  </Stack>
));
const scaleObject = z.object({
  kind: z.literal("Scale"),
  spec: z.object({ replicas: z.int().nonnegative() }),
  status: z.object({ replicas: z.int().nonnegative().optional() }).optional(),
});
const scaleResult = defineResultPreview(
  kubernetesResourcesSchema.transform((resources, context) => {
    const parsed = scaleObject.safeParse(resources.length === 1 ? resources[0] : undefined);
    if (parsed.success) return parsed.data;
    context.addIssue({ code: "custom", message: "Expected a Kubernetes Scale response" });
    return z.NEVER;
  }),
  ({ result }) => (
    <Stack gap={4}>
      <Text size="sm">Desired replicas · {result.spec.replicas}</Text>
      {result.status?.replicas !== undefined && <Text size="sm">Observed replicas · {result.status.replicas}</Text>}
    </Stack>
  )
);

function compact(
  fallbackLabel: string,
  schema: z.ZodType,
  details: NonNullable<ActionPresentation["details"]>,
  label?: ArgumentsPreview
): ActionPresentation {
  return {
    fallbackLabel,
    label,
    pane: { collapsed: details.arguments },
    details,
    offersCardDecisionButtons: (args) => schema.safeParse(args).success,
    compactCardExpands: expandsForRejectedArguments(schema),
  };
}

export const additionalKubernetesRenderers: ReadonlyArray<readonly [string, ActionPresentation]> = [
  [
    "pods_list",
    compact("List Pods across all namespaces", podSelectors, {
      arguments: allPodsFilters,
      envelopeResult: resourceListResult,
    }),
  ],
  [
    "namespaces_list",
    compact("List Kubernetes namespaces", namespaces, {
      arguments: namespaceFilters,
      envelopeResult: resourceListResult,
    }),
  ],
  [
    "projects_list",
    compact("List OpenShift projects", empty, {
      arguments: { schema: empty, render: () => null },
      envelopeResult: resourceListResult,
    }),
  ],
  [
    "nodes_log",
    compact(
      "View Node logs",
      nodeLogs,
      {
        arguments: nodeLogArguments,
        envelopeResult: kubernetesTextResult(
          "Node logs",
          "The provider reported no node logs.",
          /^The node \S+ has not logged any message yet or the log file is empty$/
        ),
      },
      nodeLogLabel
    ),
  ],
  [
    "nodes_stats_summary",
    compact(
      "Read Node resource usage",
      node,
      {
        arguments: { schema: node, render: () => null },
        result: nodeStatsResult,
      },
      nodeStatsLabel
    ),
  ],
  [
    "nodes_top",
    compact("Read Node resource usage", nodesTop, { arguments: nodesTopFilters, result: metricsResult }, nodesTopLabel),
  ],
  [
    "pods_top",
    compact("Read Pod resource usage", podsTop, { arguments: podsTopFilters, result: metricsResult }, podsTopLabel),
  ],
  [
    "configuration_view",
    compact(
      "Read Kubernetes configuration",
      configuration,
      {
        arguments: { schema: configuration, render: () => null },
        result: configResult,
      },
      configLabel
    ),
  ],
  [
    "pods_run",
    {
      fallbackLabel: "Run Kubernetes Pod",
      label: runLabel,
      pane: { opened: runArguments },
      details: { arguments: runArguments, result: kubernetesResourcesResult },
      outcome: (value) => {
        const parsed = kubernetesResourcesSchema.safeParse(value);
        return parsed.success && parsed.data.some((resource) => resource.kind === "Pod")
          ? { label: "Pod applied", failed: false }
          : undefined;
      },
      offersCardDecisionButtons: (args) => podRun.safeParse(args).success,
    },
  ],
  [
    "resources_scale",
    {
      fallbackLabel: "Read or set Kubernetes resource scale",
      label: scaleLabel,
      pane: { opened: scaleArguments },
      details: { arguments: scaleArguments, result: scaleResult },
      offersCardDecisionButtons: (args) => scale.safeParse(args).success,
    },
  ],
];
