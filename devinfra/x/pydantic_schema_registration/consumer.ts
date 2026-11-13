import type { FirstRoot, PydanticSchemaRegistrationRoots, SecondRoot } from "./generated_types";

export const firstRoot: FirstRoot = {
  shared: { label: "first", sequence: 1 },
  same_name: { first_value: "first" },
};

export const secondRoot: SecondRoot = {
  shared: { label: "second", sequence: 2 },
  same_name: { second_value: 2 },
};

export const modeRoots: PydanticSchemaRegistrationRoots = {
  first: firstRoot,
  second: secondRoot,
  validation: {
    parsed_json: '{"count":3}',
    amount: 12.34,
  },
  serialization: {
    parsed_json: { count: 3 },
    amount: "12.34",
  },
};
