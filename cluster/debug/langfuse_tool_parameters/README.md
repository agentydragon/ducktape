# Langfuse tool definitions in ClickHouse

Read-only investigation, 2026-11-10 19:14–19:34 UTC. The deployed Langfuse web and
worker images were 4.67.0. The ClickHouse installation had two application replicas,
one shard and local HDD PVCs. No row, schema, setting or storage mutation was made.

## Finding

The large August/September `model_parameters` and full-table metadata columns are
mostly historical LiteLLM tool definitions, not merely unusually verbose scalar
model settings. Historical `litellm_request` observations stored
`llm.invocation_parameters` inside the `attributes` metadata object. Langfuse's
[4.67.0 OTLP processor](https://github.com/langfuse/langfuse/blob/v4.67.0/packages/shared/src/server/otel/OtelIngestionProcessor.ts)
retains the filtered span attributes in metadata, parses `llm.invocation_parameters`
into model parameters and serializes non-scalar parameter values. The sampled
historical data is consistent with that mechanism; the exact deployed ingestion path
when those older events were originally written was not reconstructed.

A bounded sample of 150 `litellm_request` / `GENERATION` rows from 2026-08-15 was
selected with `LIMIT`, not random sampling:

- The `attributes` metadata value occupied 78.66 MiB, including 78.25 MiB of
  `llm.invocation_parameters`.
- 127 of 150 rows had a `tools` parameter. The JSON representation of those tools
  occupied 75.81 MiB in `model_parameters`; after decoding, the definitions occupied
  71.17 MiB. All 127 decoded `model_parameters.tools` values exactly matched the
  corresponding tools value in the metadata's `llm.invocation_parameters`.
- For a separate 100-row sample from the same day, both `events_full` and
  `events_core` contained the row, and all 100 complete `model_parameters` strings
  had identical SHA-256 hashes. In 94 of them, the tool-definition hashes matched.

The live `events_core_mv` selects `model_parameters` unchanged from `events_full`.
It truncates `input`, `output` and each `metadata_values` element to 200 Unicode
characters, but does not truncate `model_parameters`. The core table therefore
also stores the full tool schemas. This is a separate table per ClickHouse replica,
not another Piraeus replication layer.

The samples show a mechanism and exact equality for those rows. They do not prove
all stored tool definitions are duplicated, that every metadata attribute can be
recovered from parameters, or that the two columns have interchangeable API/search
semantics. The samples contain no exported prompt, tool-definition or response
values.

## Measured footprint and codecs

From `system.parts_columns` on one replica, summing active parts (decimal GB):

| Table and column               | Compressed | Uncompressed |
| ------------------------------ | ---------: | -----------: |
| `events_full.input`            |      50.12 |       185.20 |
| `events_full.metadata_values`  |      22.81 |       105.00 |
| `events_full.model_parameters` |      16.58 |        82.95 |
| `events_core.model_parameters` |      18.47 |        92.77 |

August accounted for 15.52 GB of compressed full metadata, 12.89 GB of compressed
full model parameters and 14.78 GB of compressed core model parameters per replica;
September was the next-largest month. These footprints overlap in content and must
not be treated as fully reclaimable bytes. There are two ClickHouse replicas.

`system.parts.default_compression_codec` reported LZ4 for all active parts of both
tables. `system.columns` showed explicit `CODEC(ZSTD(3))` on full-table `input` and
`output`, and no explicit column codec on metadata or model parameters. The
MergeTree `default_compression_codec` setting was empty. A column codec change
would need an explicit rewrite/merge plan to affect existing parts; measure
compression, CPU, I/O and spare space before proposing it.

The 52 `chat` rows observed on 2026-11-10 had 134 bytes of model parameters in
aggregate, and none exceeded 1,000 bytes. This is a current bounded observation,
not proof that every future model-call type is unaffected.

## Reproduce the aggregate measurements

Use the read-only ClickHouse client in a healthy replica. The following queries
return only sizes, counts, keys and equality, never stored values. Results are a
point-in-time snapshot; replica merges can change part sizes.

```sql
SELECT table, column,
       round(sum(column_data_compressed_bytes) / 1e9, 2) AS compressed_gb,
       round(sum(column_data_uncompressed_bytes) / 1e9, 2) AS uncompressed_gb
FROM system.parts_columns
WHERE active AND database = 'langfuse'
  AND table IN ('events_full', 'events_core')
  AND column IN ('metadata_values', 'model_parameters', 'input', 'output')
GROUP BY table, column
ORDER BY table, column;

SELECT table, partition, default_compression_codec, count() AS parts
FROM system.parts
WHERE active AND database = 'langfuse'
  AND table IN ('events_full', 'events_core')
GROUP BY table, partition, default_compression_codec
ORDER BY table, partition;

SELECT count() AS sampled,
       countIf(JSONHas(model_parameters, 'tools')) AS with_tools,
       countIf(
         JSONHas(model_parameters, 'tools') AND
         JSONExtractString(model_parameters, 'tools') = JSONExtractRaw(
           JSONExtractString(
             arrayElement(metadata_values, indexOf(metadata_names, 'attributes')),
             'llm.invocation_parameters'
           ), 'tools'
         )
       ) AS exact_tool_matches
FROM (
  SELECT model_parameters, metadata_names, metadata_values
  FROM langfuse.events_full
  WHERE project_id = 'langfuse-litellm-project'
    AND start_time >= '2026-08-15' AND start_time < '2026-08-16'
    AND name = 'litellm_request' AND type = 'GENERATION'
  LIMIT 150
)
SETTINGS max_execution_time = 25, max_threads = 1, max_bytes_to_read = 2000000000;
```

Follow-up: sample across time, event classes and large payloads; audit Langfuse
API/UI/filter and migration use of both representations; benchmark codecs on a
representative byte-weighted sample; verify an independent raw archive and replay.
Any deletion, table rewrite or altered core view needs a separate reviewed plan.
