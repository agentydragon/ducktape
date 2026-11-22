import { Anchor, Badge, Group, Stack, Text } from "@mantine/core";
import type { ReactNode } from "react";
import { z } from "zod";

import { defineCallPreview, definePreview, type ArgumentsPreview } from "./entry";
import type { ActionPresentation } from "./presentation";
import { defineResultPreview, renderResultPreview, type ResultPreview } from "./result_entry";
import { JsonBlock } from "../../ui/text_block";
import { Disclosure } from "../../../../../util/frontend/disclosure";

const origin = "https://home.allegedly.works";
const label = z.string().min(1);
const text = z.string().nullish();
const stringList = z.array(label);
const strings = z.union([label, stringList]);
const values = z.record(z.string(), z.unknown());
const parameters = z.union([values, label]).nullish();
const warnings = z.array(z.string()).nullish();

function words(input: string): string {
  return input.replaceAll("_", " ");
}

function EntityName({ id, title }: { id?: string | null; title?: string | null }): ReactNode {
  if (!id) return title ?? "Unnamed entity";
  const href = `${origin}/config/entities?search=${encodeURIComponent(id)}`;
  return (
    <Anchor href={href} target="_blank" rel="noreferrer">
      {title || id}
    </Anchor>
  );
}

function Scope({ items }: { items: readonly (readonly [string, string | number | null | undefined])[] }): ReactNode {
  return (
    <Stack gap={4}>
      {items.map(([title, value]) =>
        value === null || value === undefined || value === "" ? null : (
          <Text key={title} size="sm" style={{ overflowWrap: "anywhere" }}>
            {title} · {value}
          </Text>
        )
      )}
    </Stack>
  );
}

function Review({ children, hints }: { children: ReactNode; hints?: string[] | null }): ReactNode {
  return (
    <Stack gap="xs">
      {children}
      {hints?.map((hint, index) => (
        <Text size="sm" c="dark" key={index}>
          {hint}
        </Text>
      ))}
    </Stack>
  );
}

function extraArguments(value: unknown, consumed: readonly string[] = []): ReactNode {
  if (value == null) return null;
  if (typeof value === "string") {
    return <JsonBlock value={value} heading="Parameters" />;
  }
  const parsed = values.safeParse(value);
  if (!parsed.success) return null;
  const entries = Object.entries(parsed.data).filter(([key]) => !consumed.includes(key));
  if (!entries.length) return null;
  return (
    <Stack gap={4}>
      {entries.map(([key, item]) =>
        item === null || ["string", "number", "boolean"].includes(typeof item) ? (
          <Text size="sm" key={key} style={{ overflowWrap: "anywhere" }}>
            {words(key)} · {typeof item === "boolean" ? (item ? "Yes" : "No") : item === null ? "Empty" : String(item)}
          </Text>
        ) : (
          <JsonBlock key={key} value={item} heading={words(key)} />
        )
      )}
    </Stack>
  );
}

const entity = z.object({
  entity_id: text,
  name: text,
  original_name: text,
  friendly_name: text,
  area: text,
  area_name: text,
  state: text,
  attributes: z.object({ friendly_name: text, unit_of_measurement: text }).nullish(),
});
type Entity = z.infer<typeof entity>;

function entityName(value: Entity): string | null | undefined {
  return value.friendly_name || value.attributes?.friendly_name || value.name || value.original_name;
}

function EntityRow({ value, id }: { value: Entity; id?: string }): ReactNode {
  const name = entityName(value);
  return (
    <Group gap="xs" wrap="wrap" style={{ overflowWrap: "anywhere" }}>
      <Text size="sm" fw={600}>
        <EntityName id={value.entity_id || id} title={name} />
      </Text>
      {value.state && (
        <Badge variant="light">
          {value.state}
          {value.attributes?.unit_of_measurement && ` ${value.attributes.unit_of_measurement}`}
        </Badge>
      )}
      {(value.area_name || value.area) && (
        <Text size="xs" c="dimmed">
          {value.area_name || value.area}
        </Text>
      )}
    </Group>
  );
}

const entityResult = z.union([
  z.object({ data: entity, warnings }),
  z.object({
    states: z.record(label, entity),
    success: z.boolean().optional(),
    error_count: z.number().optional(),
    warnings,
  }),
]);
const entityStates = defineResultPreview(entityResult, ({ result }) => {
  if ("data" in result)
    return (
      <Review hints={result.warnings}>
        <EntityRow value={result.data} />
      </Review>
    );
  const rows = Object.entries(result.states);
  return (
    <Review hints={result.warnings}>
      {rows.length ? (
        rows.map(([id, value]) => <EntityRow key={id} value={value} id={id} />)
      ) : (
        <Text size="sm">No entity states returned.</Text>
      )}
      {result.error_count ? <Text size="sm">{result.error_count} state reads failed.</Text> : null}
    </Review>
  );
});
const entityDetails = defineResultPreview(
  z.object({ entity_id: label, entity_entry: entity, warnings }),
  ({ result }) => (
    <Review hints={result.warnings}>
      <EntityRow value={result.entity_entry} id={result.entity_id} />
    </Review>
  )
);

const device = z.object({
  device_id: text,
  name: text,
  name_by_user: text,
  default_name: text,
  manufacturer: text,
  model: text,
  entities: z.array(entity).nullish(),
});
const deviceResult = defineResultPreview(
  z.union([
    z.object({ device, warnings }),
    z.object({ devices: z.array(device), has_more: z.boolean().optional(), warnings }),
  ]),
  ({ result }) => {
    const entries = "device" in result ? [result.device] : result.devices;
    return (
      <Review hints={result.warnings}>
        {entries.length === 0 && <Text size="sm">No devices found.</Text>}
        {entries.map((item, index) => {
          const title = item.name_by_user || item.name || item.default_name || item.device_id || "Unnamed device";
          return (
            <Stack gap={4} key={item.device_id ?? index}>
              <Text size="sm" fw={600}>
                {item.device_id ? (
                  <Anchor
                    href={`${origin}/config/devices/device/${encodeURIComponent(item.device_id)}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {title}
                  </Anchor>
                ) : (
                  title
                )}
              </Text>
              {(item.manufacturer || item.model) && (
                <Text size="sm">{[item.manufacturer, item.model].filter(Boolean).join(" · ")}</Text>
              )}
              {item.entities?.map((value, i) => (
                <EntityRow value={value} key={value.entity_id ?? i} />
              ))}
            </Stack>
          );
        })}
        {"devices" in result && result.has_more && (
          <Text size="sm" c="dimmed">
            More devices available.
          </Text>
        )}
      </Review>
    );
  }
);

const serviceArgs = z.strictObject({
  domain: label,
  service: label,
  entity_id: strings.nullish(),
  data: parameters,
  wait: z.union([z.boolean(), label]).optional(),
  return_response: z.union([z.boolean(), label]).optional(),
  verbose: z.union([z.boolean(), label]).optional(),
  result_fields: parameters.or(stringList),
  result_attribute_keys: parameters.or(stringList),
  ws_command: text,
});
function serviceSummary(args: z.infer<typeof serviceArgs>): string {
  // Entity IDs are unambiguous even when a friendly name cannot be looked up.
  if (typeof args.entity_id !== "string") return `Call ${words(args.domain)} · ${words(args.service)}`;
  if (args.service === "turn_on" || args.service === "turn_off") {
    return `Turn ${args.service === "turn_on" ? "on" : "off"} ${args.entity_id}`;
  }
  const data = values.safeParse(args.data);
  const percentage = data.success ? data.data.percentage : undefined;
  if (args.domain === "fan" && args.service === "set_percentage" && typeof percentage === "number") {
    return `Set ${args.entity_id} to ${percentage}%`;
  }
  return `Call ${words(args.domain)} · ${words(args.service)} for ${args.entity_id}`;
}

const serviceLabel = definePreview(serviceArgs, ({ args }) => (
  <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
    {serviceSummary(args)}
  </Text>
));
const servicePreview = definePreview(serviceArgs, ({ args }) => (
  <Review>
    {(typeof args.entity_id === "string" ? [args.entity_id] : args.entity_id)?.map((id) => (
      <Text size="sm" key={id}>
        <EntityName id={id} />
      </Text>
    ))}
    {args.ws_command && <Text size="sm">WebSocket command {args.ws_command}</Text>}
    {extraArguments(args.data)}
    {(args.wait === true || args.wait === "true") && <Text size="sm">Wait for a state change when supported.</Text>}
  </Review>
));

const eventArgs = z.strictObject({ event_type: label, data: parameters });
const eventPreview = definePreview(eventArgs, ({ args }) => (
  <Review>
    <Text size="sm" fw={600}>
      Fire {words(args.event_type)}
    </Text>
    {extraArguments(args.data)}
  </Review>
));

const operation = z.object({ entity_id: label, action: label, parameters, timeout_seconds: z.number().optional() });
const bulkArgs = z.strictObject({
  action: text,
  operations: z.array(operation).or(label).nullish(),
  selector: parameters,
  parameters,
  validate_first: z.boolean().optional(),
  dry_run: z.boolean().optional(),
  parallel: z.boolean().optional(),
  timeout_seconds: z.number().nullish(),
});
const bulkPreview = definePreview(bulkArgs, ({ args }) => (
  <Review>
    {args.dry_run ? <Badge variant="light">Dry run</Badge> : null}
    {args.action && (
      <Text size="sm" fw={600}>
        {words(args.action)}
      </Text>
    )}
    {Array.isArray(args.operations)
      ? args.operations.map((item, index) => (
          <Stack key={index} gap={4}>
            <Text size="sm">
              {words(item.action)} <EntityName id={item.entity_id} />
            </Text>
            {extraArguments(item.parameters)}
            {item.timeout_seconds != null && <Text size="sm">Timeout {item.timeout_seconds}s</Text>}
          </Stack>
        ))
      : extraArguments(args.operations)}
    {extraArguments(args.selector)}
    {extraArguments(args.parameters)}
    <Text size="sm">{args.parallel === false ? "Apply in sequence" : "Apply in parallel"}</Text>
    {args.validate_first === false && <Text size="sm">Skip preflight validation</Text>}
    {args.timeout_seconds != null && <Text size="sm">Timeout {args.timeout_seconds}s</Text>}
  </Review>
));

const controlResult = defineResultPreview(
  z.object({
    success: z.boolean(),
    message: text,
    partial: z.boolean().optional(),
    warnings,
    error_count: z.number().optional(),
    result: z.unknown().optional(),
    service_response: z.unknown().optional(),
    results: z.unknown().optional(),
    summary: z.unknown().optional(),
    state_changed: z.boolean().optional(),
  }),
  ({ result }) => (
    <Review hints={result.warnings}>
      <Text size="sm" fw={600}>
        {result.message ||
          (result.partial
            ? "Partial result"
            : result.success
              ? "Home Assistant accepted the request"
              : "Home Assistant reported a failure")}
      </Text>
      {result.state_changed === false && <Text size="sm">No state change confirmed.</Text>}
      {result.error_count ? <Text size="sm">{result.error_count} operations failed.</Text> : null}
      {[
        ["Service response", result.service_response],
        ["Result", result.result ?? result.results],
        ["Operation summary", result.summary],
      ].map(([title, data]) =>
        data == null || (Array.isArray(data) && data.length === 0) ? null : (
          <Disclosure key={String(title)} summary={String(title)}>
            <JsonBlock value={data} />
          </Disclosure>
        )
      )}
    </Review>
  )
);

const entityRead = z.object({ entity_id: strings.nullish(), domain: text, platform: text, unique_id: text });
type EntityRead = z.infer<typeof entityRead>;

function entityIds(args: EntityRead): string[] {
  return typeof args.entity_id === "string" ? [args.entity_id] : (args.entity_id ?? []);
}

function EntityRequest({ args }: { args: EntityRead }): ReactNode {
  return (
    <Review>
      {entityIds(args).map((id) => (
        <Text key={id} size="sm">
          <EntityName id={id} />
        </Text>
      ))}
      <Scope
        items={[
          ["Domain", args.domain],
          ["Platform", args.platform],
          ["Registry reference", args.unique_id],
        ]}
      />
    </Review>
  );
}

const entityRequest = definePreview(entityRead, EntityRequest);

function entityCall<S extends z.ZodType>(
  resultView: ResultPreview<S>,
  namedEntities: (value: z.infer<S>) => readonly (readonly [string | null | undefined, Entity])[]
) {
  return defineCallPreview(entityRead, resultView.schema, ({ args, result }) => {
    if (result === undefined) return <EntityRequest args={args} />;
    const named = new Set(
      namedEntities(result)
        .filter(([, value]) => entityName(value))
        .map(([id]) => id)
    );
    return (
      <Review>
        <EntityRequest args={{ ...args, entity_id: entityIds(args).filter((id) => !named.has(id)) }} />
        {renderResultPreview(resultView, result)}
      </Review>
    );
  });
}

const readEntityState = entityCall(entityStates, (result) =>
  "data" in result ? [[result.data.entity_id, result.data]] : Object.entries(result.states)
);
const inspectEntity = entityCall(entityDetails, (result) => [[result.entity_id, result.entity_entry]]);

function readScope(keys: readonly (readonly [string, string])[]): ArgumentsPreview {
  return definePreview(values, ({ args }) => (
    <Scope
      items={keys.map(([field, title]) => {
        const item = args[field];
        const value = ["string", "number"].includes(typeof item)
          ? String(item)
          : Array.isArray(item)
            ? item.filter((value): value is string => typeof value === "string").join(", ")
            : undefined;
        return [title, value];
      })}
    />
  ));
}

const searches = z
  .object({
    entities: z.array(entity).nullish(),
    results: z.array(entity).nullish(),
    has_more: z.boolean().optional(),
    partial: z.boolean().optional(),
    partial_reason: text,
    warnings,
  })
  .refine((value) => value.entities != null || value.results != null);
const searchResult = defineResultPreview(searches, ({ result }) => {
  const entities = result.entities ?? result.results ?? [];
  return (
    <Review hints={result.warnings}>
      {entities.length ? (
        entities.map((value, index) => <EntityRow key={value.entity_id ?? index} value={value} />)
      ) : (
        <Text size="sm">No matching entities.</Text>
      )}
      {result.partial && <Text size="sm">{result.partial_reason ?? "Partial search results."}</Text>}
      {result.has_more && <Text size="sm">More matches available.</Text>}
    </Review>
  );
});

const floor = z.object({ name: text, areas: z.array(z.object({ name: text })).nullish() });
const topology = defineResultPreview(
  z.object({ floors: z.array(floor), unassigned_areas: z.array(z.object({ name: text })).nullish(), warnings }),
  ({ result }) => (
    <Review hints={result.warnings}>
      {result.floors.map((item, index) => (
        <Stack gap={4} key={index}>
          <Text size="sm" fw={600}>
            {item.name ?? "Unnamed floor"}
          </Text>
          <Text size="sm">
            {item.areas
              ?.map((area) => area.name)
              .filter(Boolean)
              .join(", ") || "No named areas"}
          </Text>
        </Stack>
      ))}
      {result.unassigned_areas?.length ? (
        <Text size="sm">
          Unassigned areas ·{" "}
          {result.unassigned_areas
            .map((area) => area.name)
            .filter(Boolean)
            .join(", ")}
        </Text>
      ) : null}
      {!result.floors.length && !result.unassigned_areas?.length && <Text size="sm">No floors or areas returned.</Text>}
    </Review>
  )
);

const services = defineResultPreview(
  z.object({
    services: z.record(label, z.object({ name: text, description: text })),
    has_more: z.boolean().optional(),
    warnings,
  }),
  ({ result }) => (
    <Review hints={result.warnings}>
      {Object.entries(result.services).map(([id, service]) => (
        <Stack gap={4} key={id}>
          <Text size="sm" fw={600}>
            {service.name || id}
          </Text>
          {service.description && <Text size="sm">{service.description}</Text>}
        </Stack>
      ))}
      {!Object.keys(result.services).length && <Text size="sm">No matching services.</Text>}
      {result.has_more && (
        <Text size="sm" c="dimmed">
          More services available.
        </Text>
      )}
    </Review>
  )
);

const overview = defineResultPreview(
  z.object({
    domains: z.record(
      label,
      z.union([z.number(), z.object({ count: z.number().optional(), entities: z.array(entity).optional() })])
    ),
    warnings,
  }),
  ({ result }) => (
    <Review hints={result.warnings}>
      {Object.entries(result.domains).map(([domain, value]) => (
        <Stack key={domain} gap={4}>
          <Text size="sm" fw={600}>
            {words(domain)}
            {typeof value === "number" ? ` · ${value}` : value.count != null ? ` · ${value.count}` : ""}
          </Text>
          {typeof value !== "number" &&
            value.entities?.map((item, index) => <EntityRow key={item.entity_id ?? index} value={item} />)}
        </Stack>
      ))}
      {!Object.keys(result.domains).length && <Text size="sm">No entities in this overview.</Text>}
    </Review>
  )
);

function presentation(label: string, args: ArgumentsPreview, result: ResultPreview): ActionPresentation {
  return { fallbackLabel: label, pane: { opened: args }, details: { arguments: args, result } };
}

export const homeAssistantRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  [
    "ha_call_service",
    { ...presentation("Call Home Assistant service", servicePreview, controlResult), label: serviceLabel },
  ],
  ["ha_call_event", presentation("Fire Home Assistant event", eventPreview, controlResult)],
  ["ha_bulk_control", presentation("Control Home Assistant devices", bulkPreview, controlResult)],
  ["ha_get_state", { ...presentation("Read entity state", entityRequest, entityStates), call: readEntityState }],
  [
    "ha_get_entity",
    { ...presentation("Inspect Home Assistant entity", entityRequest, entityDetails), call: inspectEntity },
  ],
  [
    "ha_get_device",
    presentation(
      "Inspect Home Assistant devices",
      readScope([
        ["device_id", "Device reference"],
        ["entity_id", "Entity"],
        ["integration", "Integration"],
        ["area_id", "Area"],
        ["manufacturer", "Manufacturer"],
        ["limit", "Limit"],
      ]),
      deviceResult
    ),
  ],
  [
    "ha_search",
    presentation(
      "Search Home Assistant",
      readScope([
        ["query", "Search"],
        ["search_types", "Kinds"],
        ["domain_filter", "Domain"],
        ["area_filter", "Area"],
        ["state_filter", "State"],
        ["limit", "Limit"],
      ]),
      searchResult
    ),
  ],
  [
    "ha_get_overview",
    presentation(
      "Read Home Assistant overview",
      readScope([
        ["domains", "Domains"],
        ["limit", "Limit"],
        ["max_entities_per_domain", "Limit per domain"],
      ]),
      overview
    ),
  ],
  [
    "ha_list_floors_areas",
    presentation(
      "List floors and areas",
      definePreview(
        z.strictObject({
          fields: z.unknown().optional(),
          area_fields: z.unknown().optional(),
        }),
        () => <Text size="sm">Show the Home Assistant room layout.</Text>
      ),
      topology
    ),
  ],
  [
    "ha_list_services",
    presentation(
      "List Home Assistant services",
      readScope([
        ["domain", "Domain"],
        ["query", "Search"],
        ["limit", "Limit"],
      ]),
      services
    ),
  ],
]);
