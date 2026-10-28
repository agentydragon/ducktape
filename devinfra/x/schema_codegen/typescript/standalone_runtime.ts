import { z } from "zod";

import type { SearchArguments } from "./standalone_types";
import standaloneSchema from "./standalone.schema.json";

type JsonSchema = Parameters<typeof z.fromJSONSchema>[0];

const searchArgumentsSchema = z.fromJSONSchema(standaloneSchema as unknown as JsonSchema);

export function validateSearchArguments(input: unknown): input is SearchArguments {
  return searchArgumentsSchema.safeParse(input).success;
}
