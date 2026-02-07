-- OpenAI Probe Results Schema for TimescaleDB

-- Recreate table with JSONB payloads (drop old data)
DROP TABLE IF EXISTS probe_results;

CREATE TABLE probe_results (
  -- Timing
  start_time TIMESTAMPTZ NOT NULL,
  end_time TIMESTAMPTZ NOT NULL,
  latency_s DOUBLE PRECISION NOT NULL,

  -- Model info
  model TEXT NOT NULL,
  family TEXT NOT NULL,
  kind TEXT NOT NULL,  -- 'chat' or 'responses'

  -- Result
  success BOOLEAN NOT NULL,

  -- Error details (NULL if success)
  error_code TEXT,      -- 'TIMEOUT', 'NO-CAP', 'NOT-CHAT', 'SERVER-ERROR', etc
  error_status INT,     -- HTTP status code
  error_body JSONB,     -- Raw error body from API (structured)

  -- Chat API response fields (NULL unless kind='chat' and success=true)
  chat_response JSONB,           -- Full chat API response payload

  -- Responses API fields (NULL unless kind='responses' and success=true)
  responses_body JSONB,           -- Full responses API payload

  -- Request metadata
  request_id TEXT,
  api_key_suffix TEXT  -- Last 3 chars of key used
);

-- Create hypertable partitioned by start_time
SELECT create_hypertable('probe_results', 'start_time');

-- Indexes for common queries
CREATE INDEX ON probe_results (model, start_time DESC);
CREATE INDEX ON probe_results (family, kind, start_time DESC);
CREATE INDEX ON probe_results (success, start_time DESC) WHERE NOT success;
CREATE INDEX ON probe_results (error_code, start_time DESC) WHERE error_code IS NOT NULL;

-- Retention policy: Keep 30 days of data
SELECT add_retention_policy('probe_results', INTERVAL '30 days');
