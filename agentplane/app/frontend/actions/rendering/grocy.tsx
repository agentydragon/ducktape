import { Stack, Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import { Chip } from "./chips";
import { definePreview, type ArgumentsPreview } from "./entry";
import { defineResultPreview, type ResultPreview } from "./result_entry";

const listArguments = z.strictObject({ detail: z.enum(["brief", "full"]).optional() });

function ListOptions({ args, noun }: { args: z.infer<typeof listArguments>; noun: string }): JSX.Element {
  const detail = args.detail ?? "brief";
  return <Text size="sm">{detail === "full" ? `Full ${noun} records` : `${noun} names`}</Text>;
}

function ProductListOptions({ args }: { args: z.infer<typeof listArguments> }): JSX.Element {
  return <ListOptions args={args} noun="Product" />;
}

function ProductListLabel({ args }: { args: z.infer<typeof listArguments> }): JSX.Element {
  void args;
  return (
    <Text size="sm" fw={600}>
      List Grocy products
    </Text>
  );
}

function QuantityUnitListOptions({ args }: { args: z.infer<typeof listArguments> }): JSX.Element {
  return <ListOptions args={args} noun="Quantity unit" />;
}

function QuantityUnitListLabel({ args }: { args: z.infer<typeof listArguments> }): JSX.Element {
  void args;
  return (
    <Text size="sm" fw={600}>
      List quantity units
    </Text>
  );
}

function SystemInfoLabel({ args }: { args: z.infer<typeof noArguments> }): JSX.Element {
  void args;
  return (
    <Text size="sm" fw={600}>
      Show Grocy system information
    </Text>
  );
}

export const productsListLabel: ArgumentsPreview = definePreview(listArguments, ProductListLabel);
export const productsListArguments: ArgumentsPreview = definePreview(listArguments, ProductListOptions);
export const quantityUnitsListLabel: ArgumentsPreview = definePreview(listArguments, QuantityUnitListLabel);
export const quantityUnitsListArguments: ArgumentsPreview = definePreview(listArguments, QuantityUnitListOptions);

const noArguments = z.strictObject({});

function SystemInfoRequest(): JSX.Element {
  return (
    <Text size="sm" c="dimmed">
      No arguments.
    </Text>
  );
}

export const systemInfoLabel: ArgumentsPreview = definePreview(noArguments, SystemInfoLabel);
export const systemInfoArguments: ArgumentsPreview = definePreview(noArguments, SystemInfoRequest);

const namedRows = z.array(z.object({ id: z.number(), name: z.string() }).passthrough());

function NamedRows({ result }: { result: z.infer<typeof namedRows> }): JSX.Element {
  return (
    <Stack gap={4}>
      <Text fw={600}>{result.length} found</Text>
      {result.map((row) => (
        <Text key={row.id} size="sm">
          {row.name}{" "}
          <Text span c="dimmed">
            #{row.id}
          </Text>
        </Text>
      ))}
    </Stack>
  );
}

export const productsListResult: ResultPreview = defineResultPreview(namedRows, NamedRows);
export const quantityUnitsListResult: ResultPreview = defineResultPreview(namedRows, NamedRows);

const systemInfo = z
  .object({
    grocy_version: z
      .object({ Version: z.string(), ReleaseDate: z.string().nullable().optional() })
      .nullable()
      .optional(),
    php_version: z.string().nullable().optional(),
    sqlite_version: z.string().nullable().optional(),
    db_version: z.union([z.string(), z.number()]).nullable().optional(),
    os: z.string().nullable().optional(),
    client: z.string().nullable().optional(),
  })
  .passthrough();

function SystemInfo({ result }: { result: z.infer<typeof systemInfo> }): JSX.Element {
  const fields: Array<[string, string]> = [];
  if (result.grocy_version) {
    fields.push([
      "Grocy version",
      result.grocy_version.ReleaseDate
        ? `${result.grocy_version.Version} (${result.grocy_version.ReleaseDate})`
        : result.grocy_version.Version,
    ]);
  }
  if (result.php_version) fields.push(["PHP version", result.php_version]);
  if (result.sqlite_version) fields.push(["SQLite version", result.sqlite_version]);
  if (result.db_version != null) fields.push(["Database version", String(result.db_version)]);
  if (result.os) fields.push(["OS", result.os]);
  if (result.client) fields.push(["Client", result.client]);
  return (
    <Stack gap="xs">
      {fields.map(([label, value]) => (
        <Chip key={label} label={label} value={value} />
      ))}
      {fields.length === 0 && (
        <Text size="sm" c="dimmed">
          No system details returned.
        </Text>
      )}
    </Stack>
  );
}

export const systemInfoResult: ResultPreview = defineResultPreview(systemInfo, SystemInfo);
