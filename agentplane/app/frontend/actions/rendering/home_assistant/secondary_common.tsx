import { Badge, Code, Group, Paper, Stack, Text } from "@mantine/core";
import type { JSX, ReactNode } from "react";
import { z } from "zod";

import { Disclosure, DisclosureBody } from "../../../../../../util/frontend/disclosure";
import { JsonBlock, TextBlock } from "../../../ui/text_block";
import { definePreview } from "../entry";
import type { ActionOutcome, ActionPresentation } from "../presentation";
import { defineResultPreview, type ResultPreview } from "../result_entry";

export type HaRecord = Record<string, unknown>;
const record = z.record(z.string(), z.unknown());

export function asRecord(value: unknown): HaRecord | undefined {
  const parsed = record.safeParse(value);
  return parsed.success ? parsed.data : undefined;
}

const acronyms: Readonly<Record<string, string>> = {
  api: "API",
  cpu: "CPU",
  id: "ID",
  knx: "KNX",
  rrule: "Recurrence rule",
  ssh: "SSH",
  tts: "TTS",
  stt: "STT",
  uid: "UID",
  url: "URL",
  ws: "WebSocket",
};

export function humanField(key: string): string {
  const words = key
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/[_-]+/g, " ")
    .split(" ");
  return words
    .map((part, index) => acronyms[part] ?? (index === 0 ? part.charAt(0).toUpperCase() + part.slice(1) : part))
    .join(" ");
}

function printable(value: unknown): string | undefined {
  if (typeof value === "string") return value;
  if (typeof value === "number") return String(value);
  if (typeof value === "boolean") return value ? "Yes" : "No";
  const date = asRecord(value);
  if (typeof date?.date === "string") return date.date;
  if (typeof date?.dateTime === "string") return date.dateTime;
  return undefined;
}

export function HaField({ name, children }: { name: string; children: ReactNode }): JSX.Element {
  return (
    <Text size="sm" style={{ overflowWrap: "anywhere", whiteSpace: "pre-wrap" }}>
      <Text span fw={600} size="sm">
        {name}
      </Text>
      {" · "}
      {children}
    </Text>
  );
}

const codeFields = new Set(["python_transform", "template", "content", "config"]);
const pagination = new Set(["offset", "config_hash", "diagnostics_data_offset", "message_offset"]);

function Argument({ field, value }: { field: string; value: unknown }): ReactNode {
  const name =
    (
      {
        wait: "Wait for verification",
        require_admin: "Require administrator access",
        show_in_sidebar: "Show in sidebar",
      } as Record<string, string>
    )[field] ?? humanField(field);
  if (value === null) return <HaField name={name}>Null</HaField>;
  if (field === "config_hash" && typeof value === "string") {
    return <Text size="sm">Require the saved configuration version to match.</Text>;
  }
  if (pagination.has(field) && value === 0) return null;
  if (codeFields.has(field)) {
    const exact =
      typeof value === "string" ? (
        <TextBlock
          heading={name}
          text={value}
          language={field === "python_transform" ? "python" : field === "config" ? "yaml" : undefined}
        />
      ) : (
        <JsonBlock heading={name} value={value} />
      );
    const config = field === "config" ? asRecord(value) : undefined;
    if (!config) return exact;
    const sections = [
      ["trigger", config.triggers ?? config.trigger],
      ["condition", config.conditions ?? config.condition],
      ["action", config.actions ?? config.action ?? config.sequence],
      ["view", config.views],
    ].flatMap(([label, entries]) =>
      Array.isArray(entries) ? [`${entries.length} ${String(label)}${entries.length === 1 ? "" : "s"}`] : []
    );
    const summary = [printable(config.alias) ?? printable(config.title), ...sections].filter(Boolean);
    return summary.length ? (
      <Stack gap={4}>
        <HaField name="Configuration">{summary.join(" · ")}</HaField>
        <Disclosure summary={<Text size="sm">Show configuration</Text>}>
          <DisclosureBody>{exact}</DisclosureBody>
        </Disclosure>
      </Stack>
    ) : (
      exact
    );
  }
  const plain = printable(value);
  if (plain !== undefined) {
    return (
      <HaField name={name}>{plain === "" ? "Empty value" : field === "action" ? humanField(plain) : plain}</HaField>
    );
  }
  if (Array.isArray(value) && value.every((item) => printable(item) !== undefined)) {
    return <HaField name={name}>{value.length === 0 ? "Empty list" : value.map(printable).join(", ")}</HaField>;
  }
  return <JsonBlock heading={name} value={value} />;
}

export interface HaResultSpec {
  fields?: readonly string[];
  rows?: readonly string[];
  text?: readonly string[];
  objects?: readonly string[];
  reportsPhase?: boolean;
  outcome?: string;
}

const titleFields = [
  "friendly_name",
  "title",
  "summary",
  "name",
  "alias",
  "entity_id",
  "domain",
  "run_id",
  "operation_id",
  "device_id",
  "uid",
];
const rowFields = [
  "state",
  "status",
  "value",
  "start",
  "end",
  "start_time",
  "time",
  "last_changed",
  "last_updated",
  "timestamp",
  "when",
  "trigger",
  "theme",
  "default_theme",
  "default_dark_theme",
  "connection_status",
  "home_assistant_version",
  "ha_mcp_version",
  "entity_count",
  "condition",
  "action",
  "radius",
  "latitude",
  "longitude",
  "floor_id",
  "updated_fields",
  "result",
  "message",
  "description",
  "level",
  "execution",
  "location",
  "due",
  "due_date",
  "due_datetime",
  "version",
  "installed_version",
  "latest_version",
  "available_version",
  "count",
  "total",
  "mean",
  "min",
  "max",
  "sum",
  "change",
  "exposed_to",
  "error",
  "final_state",
  "target_state",
  "mode",
  "enabled",
  "icon",
  "path",
  "type",
  "url",
] as const;

function rowTitle(entry: HaRecord): string | undefined {
  return titleFields.find((key) => typeof entry[key] === "string" && entry[key] !== "");
}

function simpleFields(entry: HaRecord, keys: readonly string[]): JSX.Element[] {
  return keys.flatMap((key) => {
    const value = entry[key];
    const interval = ["period", "time_range", "timestamp"].includes(key) ? asRecord(value) : undefined;
    const intervalText =
      typeof interval?.start === "string"
        ? [interval.start, interval.end ?? interval.finish]
            .filter((part): part is string => typeof part === "string")
            .join(" – ")
        : undefined;
    const printableValue =
      intervalText ??
      ((key === "action" || key === "status" || key === "mode") &&
      typeof value === "string" &&
      /^[a-z][a-z_ -]*$/.test(value)
        ? humanField(value)
        : printable(value));
    if (printableValue !== undefined && printableValue !== "")
      return [
        <HaField key={key} name={humanField(key)}>
          {printableValue}
        </HaField>,
      ];
    if (Array.isArray(value) && value.length > 0 && value.every((item) => printable(item) !== undefined)) {
      return [
        <HaField key={key} name={humanField(key)}>
          {value.map(printable).join(", ")}
        </HaField>,
      ];
    }
    return [];
  });
}

function RecordRow({ entry }: { entry: HaRecord }): JSX.Element {
  const title = rowTitle(entry);
  const fields = simpleFields(
    entry,
    rowFields.filter((key) => key !== title)
  );
  const nested = [
    "states",
    "statistics",
    "values",
    "entries",
    "actions",
    "triggers",
    "conditions",
    "sequence",
    "views",
    "cards",
    "backups",
    "devices",
    "pipelines",
  ].filter((key) => Array.isArray(entry[key]));
  const error = asRecord(entry.error);
  const details = fields.length > 0 || nested.length > 0 || error !== undefined;
  return (
    <Paper withBorder p="xs">
      <Stack gap={3}>
        {title && (
          <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
            {String(entry[title])}
          </Text>
        )}
        {fields}
        {error && simpleFields(error, ["message", "code"])}
        {entry.has_more === true && (
          <Text size="xs" c="dimmed">
            More results are available.
          </Text>
        )}
        {nested.map((key) => (
          <ResultRows key={key} heading={humanField(key)} value={entry[key]} />
        ))}
        {!title && !details && (
          <Disclosure summary={<Text size="sm">Recorded entry</Text>}>
            <DisclosureBody>
              <JsonBlock value={entry} />
            </DisclosureBody>
          </Disclosure>
        )}
      </Stack>
    </Paper>
  );
}

function ResultRows({ heading, value }: { heading: string; value: unknown }): JSX.Element | null {
  const object = asRecord(value);
  const rows = Array.isArray(value)
    ? value
    : object &&
      Object.entries(object).map(([name, row]) => {
        const detail = asRecord(row);
        return detail ? { name, ...detail } : { name, value: row };
      });
  if (!rows) return null;
  const content = rows.map((row, index) => {
    const entry = asRecord(row);
    return entry ? (
      <RecordRow key={index} entry={entry} />
    ) : (
      <Text key={index} size="sm" style={{ overflowWrap: "anywhere" }}>
        {printable(row) ?? JSON.stringify(row)}
      </Text>
    );
  });
  return (
    <Stack gap={4}>
      <Text size="sm" fw={600}>
        {heading}
      </Text>
      {rows.length === 0 && (
        <Text size="sm" c="dimmed">
          No entries returned.
        </Text>
      )}
      {content.slice(0, 10)}
      {content.length > 10 && (
        <Disclosure summary={<Text size="sm">Show {content.length - 10} more</Text>}>
          <DisclosureBody>
            <Stack gap={4}>{content.slice(10)}</Stack>
          </DisclosureBody>
        </Disclosure>
      )}
    </Stack>
  );
}

function phase(value: unknown): ActionOutcome | undefined {
  if (value === "pending") return { label: "Pending", failed: false };
  if (value === "queued") return { label: "Queued", failed: false };
  if (value === "completed") return { label: "Completed", failed: false };
  if (value === "timeout") return { label: "Timed out", failed: true };
  if (value === "failed") return { label: "Failed", failed: true };
  return undefined;
}

export function haResult(spec: HaResultSpec): ResultPreview {
  const displayed = [...(spec.fields ?? []), ...(spec.rows ?? []), ...(spec.text ?? []), ...(spec.objects ?? [])];
  const resultSchema = record.refine(
    (value) =>
      value.success === false ||
      displayed.some((key) => key in value) ||
      (spec.outcome !== undefined && value.success === true)
  );
  return defineResultPreview(resultSchema, ({ result }) => {
    const status = phase(result.status);
    const messages = typeof result.message === "string" ? result.message : undefined;
    const printed = simpleFields(
      result,
      (spec.fields ?? []).filter(
        (key) =>
          (key !== "status" || status === undefined) &&
          (key !== "message" || messages === undefined) &&
          !spec.rows?.includes(key) &&
          !spec.text?.includes(key) &&
          !spec.objects?.includes(key)
      )
    );
    const error = asRecord(result.error);
    return (
      <Stack gap="xs">
        {status && !spec.reportsPhase && (
          <Group>
            <Badge color={status.failed ? "red" : "gray"} variant="light">
              {status.label}
            </Badge>
          </Group>
        )}
        {messages && (
          <Text size="sm" style={{ overflowWrap: "anywhere" }}>
            {messages}
          </Text>
        )}
        {printed}
        {typeof result.error === "string" && <HaField name="Error">{result.error}</HaField>}
        {error && simpleFields(error, ["message", "code"])}
        {(spec.text ?? []).map(
          (key) =>
            typeof result[key] === "string" && <TextBlock key={key} heading={humanField(key)} text={result[key]} />
        )}
        {(spec.objects ?? []).map((key) => {
          const entry = asRecord(result[key]);
          return entry && <RecordRow key={key} entry={entry} />;
        })}
        {(spec.rows ?? []).map((key) => (
          <ResultRows key={key} heading={humanField(key)} value={result[key]} />
        ))}
        {result.has_more === true && (
          <Text size="sm" c="dimmed">
            More results are available.
          </Text>
        )}
        <Disclosure summary={<Text size="sm">Provider details</Text>}>
          <DisclosureBody>
            <JsonBlock value={result} />
          </DisclosureBody>
        </Disclosure>
      </Stack>
    );
  });
}

export interface HaActionSpec {
  title: string;
  modeTitle?: (args: HaRecord) => string;
  target?: readonly string[];
  fields: readonly string[];
  required?: readonly string[];
  result?: HaResultSpec;
  envelopeResult?: ResultPreview;
}

export function haAction(spec: HaActionSpec): ActionPresentation {
  const known = new Set(spec.fields);
  const required = spec.required ?? [];
  const target = spec.target ?? [];
  const schema = record.refine(
    (value) => Object.keys(value).every((key) => known.has(key)) && required.every((key) => value[key] != null)
  );
  const requestFields = (args: HaRecord) =>
    spec.fields.filter((field) => field in args && (!target.includes(field) || printable(args[field]) === undefined));
  const preview = definePreview(schema, ({ args }) => {
    const fields = requestFields(args);
    return fields.length ? (
      <Stack gap={4}>
        {fields.map((field) => (
          <Argument key={field} field={field} value={args[field]} />
        ))}
      </Stack>
    ) : null;
  });
  return {
    fallbackLabel: spec.title,
    label: definePreview(schema, ({ args }) => {
      const targets = target.flatMap((key) => printable(args[key]) ?? []).filter(Boolean);
      return (
        <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
          {spec.modeTitle?.(args) ?? spec.title}
          {targets.length > 0 && (
            <>
              {" "}
              · <Code>{targets.join(" / ")}</Code>
            </>
          )}
        </Text>
      );
    }),
    pane: { opened: preview },
    details: {
      arguments: preview,
      result: spec.result && haResult(spec.result),
      envelopeResult: spec.envelopeResult,
    },
    outcome: (value) => {
      const response = asRecord(value);
      if (response?.success === false) return { label: "Provider reported failure", failed: true };
      if (response?.partial === true) return { label: "Partial result", failed: false };
      if (spec.result?.reportsPhase) {
        if (response?.long_running === true) return { label: "Operation started", failed: false };
        const reported = phase(response?.status);
        if (reported !== undefined) return reported;
      }
      if (spec.result?.outcome && response?.success === true) {
        return { label: spec.result.outcome, failed: false };
      }
      return undefined;
    },
    compactCardExpands: (value) => {
      const parsed = schema.safeParse(value);
      return !parsed.success || requestFields(parsed.data).length > 0;
    },
  };
}
