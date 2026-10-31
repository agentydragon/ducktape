-- Exact provider/model labels from result.modelUsage metadata only.
-- No prompts, result text, or tool content are read or returned.
BEGIN READ ONLY;
SET LOCAL statement_timeout = '60s';

SELECT json_build_object(
    'model', model_usage.model,
    'provider', model_usage.value->>'provider',
    'cost_basis', model_usage.value->>'costBasis',
    'canonical_model', model_usage.value->>'canonicalModel',
    'result_rows', count(*)
)::text
FROM events e
CROSS JOIN LATERAL jsonb_each(
    CASE WHEN jsonb_typeof(e.payload->'modelUsage') = 'object' THEN e.payload->'modelUsage' ELSE '{}'::jsonb END
) AS model_usage(model, value)
WHERE e.event_type = 'result'
GROUP BY model_usage.model, model_usage.value->>'provider', model_usage.value->>'costBasis', model_usage.value->>'canonicalModel'
ORDER BY model_usage.model,
         model_usage.value->>'provider',
         model_usage.value->>'costBasis',
         model_usage.value->>'canonicalModel';

COMMIT;
