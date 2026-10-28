import { fromJson, toJson } from "@bufbuild/protobuf";

import {
  SampleSchema,
  type Sample,
  type SampleJson,
} from "./proto_es_generated/devinfra/x/schema_codegen/protobuf/sample_pb.js";

export function decodeSample(value: SampleJson): Sample {
  return fromJson(SampleSchema, value);
}

export function encodeSample(value: Sample): SampleJson {
  return toJson(SampleSchema, value);
}
