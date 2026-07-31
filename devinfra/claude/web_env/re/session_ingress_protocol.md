# Session Ingress API Protocol Specification

**Reverse engineered from:** `environment-manager` binary (Build ID: a6f96673, Go 1.25.7)

> **Re-verified against Build ID `0b86a2a0` (`release-1186d93b9-ext`), 2026-07.**
> The event payload types are **unchanged** in that release: every struct in the
> session-ingress package has the same fields, json tags and byte offsets as in
> the previous binary (`release-d84d76b7-ext`). Two corrections to this document
> came out of that check, both of which were already wrong before:
>
> - The event envelope's identifier field is **`uuid`**, not `id`, and the
>   envelope carries far more than `{type, id, data}` — see the table below.
> - `EnvManagerLogEventData`'s shape (`message`/`level`/`source`/`timestamp`/
>   `nanos`/`fields`) is **not** corroborated by any struct in either binary;
>   the only `nanos` json tag present belongs to `google.protobuf.Timestamp`.
>   Treat the `env_manager_log` example below as unverified.
>
> Field-level evidence (RTTI addresses) is in
> <environment*manager/src/internal/api/session_ingress_types.go>.
> The **session-context / task-run** wire format — a different contract that
> \_did* change in this release — is in
> <environment_manager/src/internal/api/session_context_types.go>.

**Base URL:** `https://api.anthropic.com`

**Authentication:** Bearer token via `Authorization` header

## API Versions

The API supports two versions selected via the `UseV2` flag:

- **v1**: `/v1/session_ingress/...`
- **v2**: `/v2/session_ingress/...`

## Common Headers

All requests include:

```
Authorization: Bearer <session_ingress_token>
Content-Type: application/json
X-Environment-Manager-Version: <util.Version>
```

OpenTelemetry trace context is injected via `propagation.HeaderCarrier`.

## Endpoints

### 1. POST `/v{1|2}/session_ingress/session/{sessionID}/events`

**Purpose:** Post session ingress events (assistant messages, logs, results)

**Request Format:**

```json
{
  "events": [
    {
      "type": "env_manager_log" | "assistant" | "result",
      "id": "<uuid>",
      "data": <EventData>
    }
  ]
}
```

**Content-Type Header Value:** `session_event`

**Event Types:**

#### a) `env_manager_log`

```json
{
  "type": "env_manager_log",
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "data": {
    "message": "Log message text",
    "level": "debug" | "info" | "warn" | "error",
    "source": "source identifier",
    "timestamp": "2006-01-02T15:04:05.000000000Z07:00",
    "nanos": 0,
    "fields": {
      "key1": "value1",
      "key2": "value2"
    }
  }
}
```

#### b) `assistant` (Synthetic Assistant Message)

```json
{
  "type": "assistant",
  "id": "550e8400-e29b-41d4-a716-446655440001",
  "data": {
    "content": [
      {
        "type": "text",
        "text": "Assistant message content"
      }
    ]
  }
}
```

#### c) `result` (Session Completion/Error)

```json
{
  "type": "result",
  "id": "550e8400-e29b-41d4-a716-446655440002",
  "data": {
    "error": null | {<SessionError>},
    "is_error": false | true,
    "has_error": false | true,
    "session_error": null | <error details>
  }
}
```

**Success Response:** HTTP 200 OK

**Error Responses:**

- HTTP 501 Not Implemented → Returns `ErrEndpointNotImplemented`
- HTTP 4xx/5xx → Returns error with status code and response body

**Client Logging:**

- Before request: Debug level - "posting session ingress event"
- After success: Warn level - "posted session ingress event with nil error"

---

### 2. POST `/v{1|2}/session_ingress/session/{sessionID}/diag_logs`

**Purpose:** Forward diagnostic log entries

**Request Format:**

```json
{
  "items": [
    {
      "timestamp": "2006-01-02T15:04:05.000000000Z07:00",
      "field1": "value1",
      "field2": "value2",
      ...
    }
  ]
}
```

**Content-Type Header Value:** `diag_logs`

**Wire Format Conversion:**

- Each `DiagLogEntry` is converted to a map
- Timestamp formatted as: `"2006-01-02T15:04:05.000000000Z07:00"`
- All fields from `DiagLogEntry.Fields` are copied to the map
- Timestamp is added as a key in the map

**Success Response:** HTTP 200 OK

**Special Handling:**

- If endpoint returns HTTP 501, logs are silently discarded
- Binary contains string: "Diag logs endpoint not implemented; discarding logs"

**Client Logging:**

- Before request: Warn level - "forwarding diag logs"
- After success: Warn level - "posted diag logs with nil error"

---

### 3. POST `/v{1|2}/session_ingress/session/{sessionID}/session_event`

**Purpose:** Post session activity events

**Request Format:**

```json
{
  "category": "<LogCategory>",
  "event_type": "<string>"
}
```

**Content-Type Header Value:** `session_event`

**Success Response:** HTTP 200 OK

---

### 4. POST `/v{1|2}/session_ingress/session/{sessionID}/synthetic_assistant`

**Purpose:** Post synthetic assistant message events

**Request Format:**

```json
{
  "message": "<string>"
}
```

**Content-Type Header Value:** `synthetic_assistant`

**Success Response:** HTTP 200 OK

---

### 5. POST `/v{1|2}/session_ingress/session/{sessionID}/result`

**Purpose:** Post result events

**Request Format:**

```json
{
  "category": "<LogCategory>",
  "event_type": "<string>"
}
```

**Content-Type Header Value:** `result`

**Success Response:** HTTP 200 OK

---

## HTTP Client Behavior

**Retries:** All requests use `RetryableHTTPDo` which implements retry logic

**Error Handling:**

1. JSON marshal errors → `"failed to marshal JSON for %s: %w"`
2. Request creation errors → `"failed to create request for %s: %w"`
3. HTTP errors → `"request to %s failed: %w"`
4. HTTP 501 responses → Wrapped `ErrEndpointNotImplemented` with details
5. Other non-200 responses → `"unexpected response from %s: status=%d, body=%s"`

**Response Body Reading:**

- Always reads response body for error reporting
- Ignores errors when reading response body

---

## URL Construction

**Format:** `{baseURL}/{version}/session_ingress/session/{escapedSessionID}/{action}`

Where:

- `baseURL`: From `HttpClient.BaseURL`
- `version`: `"v1"` or `"v2"` based on `UseV2` flag
- `escapedSessionID`: URL-escaped session ID via `url.PathEscape()`
- `action`: Endpoint action string (e.g., `"events"`, `"diag_logs"`)

**Example:**

```
https://api.anthropic.com/v2/session_ingress/session/session_01HCsnGQoHJrmVYVEWJfVNNo/events
```

---

## Data Types

### SessionIngressEvent

Verified against Build ID `0b86a2a0` RTTI (`fHxyBOR9qvy.AIGJ5cph`, VMA
`0x28eaa00`, size `0xc0`, 16 fields); identical in the previous binary
(`viRrDTePbcGS.J4sedR`, VMA `0x247a760`).

```go
type SessionIngressEvent struct {
    Type              string             `json:"type"`                         // +0x00
    UUID              string             `json:"uuid"`                         // +0x10
    Data              EventData          `json:"data,omitempty"`               // +0x20
    Message           MessageContent     `json:"message,omitempty"`            // +0x30
    ParentToolUseID   *string            `json:"parent_tool_use_id,omitempty"` // +0x40
    IsAPIErrorMessage *bool              `json:"isApiErrorMessage,omitempty"`  // +0x48
    Subtype           *string            `json:"subtype,omitempty"`            // +0x50
    IsError           *bool              `json:"is_error,omitempty"`           // +0x58
    DurationMs        *int               `json:"duration_ms,omitempty"`        // +0x60
    DurationAPIMs     *int               `json:"duration_api_ms,omitempty"`    // +0x68
    NumTurns          *int               `json:"num_turns,omitempty"`          // +0x70
    TotalCostUSD      *float64           `json:"total_cost_usd,omitempty"`     // +0x78
    Errors            []string           `json:"errors,omitempty"`             // +0x80
    ModelUsage        ModelUsage         `json:"modelUsage,omitempty"`         // +0x98
    PermissionDenials []PermissionDenial `json:"permission_denials,omitempty"` // +0xa0
    Usage             *Usage             `json:"usage,omitempty"`              // +0xb8
}
```

Supporting types, all RTTI-verified and all unchanged between the two binaries:

```go
// fHxyBOR9qvy.Labalu1jJu @0x28a9c00
type AssistantMessage struct {
    Role       string         `json:"role"`
    Model      string         `json:"model"`
    Content    []ContentBlock `json:"content"`
    StopReason string         `json:"stop_reason"`
    Usage      Usage          `json:"usage"`
}

// fHxyBOR9qvy.TRepBeaLEAWm @0x2883260
type Usage struct {
    InputTokens              int `json:"input_tokens"`
    OutputTokens             int `json:"output_tokens"`
    CacheCreationInputTokens int `json:"cache_creation_input_tokens,omitempty"`
    CacheReadInputTokens     int `json:"cache_read_input_tokens,omitempty"`
}

// fHxyBOR9qvy.Ai2IVr3VUa @0x27e77e0
type PermissionDenial struct {
    ToolName    string    `json:"tool_name"`
    Reason      string    `json:"reason"`
    RequestedAt time.Time `json:"requested_at"`
}

// fHxyBOR9qvy.C55iIMbPNU @0x28a9d00 — the session activity / diag record
type SessionActivityEntry struct {
    Level     LogLevel               `json:"level"`
    Category  LogCategory            `json:"category"`
    Content   string                 `json:"content"`
    Timestamp time.Time              `json:"timestamp"`
    Extra     map[string]interface{} `json:"extra"`
}
```

### EnvManagerLogEventData

```go
type EnvManagerLogEventData struct {
    Message   string            `json:"message"`
    Level     string            `json:"level"`
    Source    string            `json:"source"`
    Timestamp time.Time         `json:"timestamp"`
    Nanos     int64             `json:"nanos"`
    Fields    map[string]string `json:"fields"`
}
```

### DiagLogEntry

```go
type DiagLogEntry struct {
    Timestamp time.Time
    Fields    map[string]interface{}
}
```

### ContentBlock

```go
type ContentBlock struct {
    Type string `json:"type"`
    Text string `json:"text"`
}
```

---

## Security Notes

1. **Token Storage:** Session ingress token stored at `/home/claude/.claude/remote/.session_ingress_token` (root-only readable, mode 0600)

2. **Authentication:** All requests require valid Bearer token in Authorization header

3. **Write Access:** Token grants write access to session - can inject events, logs, and synthetic messages

4. **No Read Access:** Protocol is write-only - no endpoints for reading session state

---

## Protocol Usage Examples

### Send a log event:

```bash
curl -X POST \
  "https://api.anthropic.com/v2/session_ingress/session/session_01ABC/events" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -H "X-Environment-Manager-Version: 1.0.0" \
  -d '{
    "events": [{
      "type": "env_manager_log",
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "data": {
        "message": "Custom log message",
        "level": "info",
        "source": "external-tool",
        "timestamp": "2026-02-14T12:00:00Z",
        "nanos": 0,
        "fields": {"custom_field": "value"}
      }
    }]
  }'
```

### Send diagnostic logs:

```bash
curl -X POST \
  "https://api.anthropic.com/v2/session_ingress/session/session_01ABC/diag_logs" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -H "X-Environment-Manager-Version: 1.0.0" \
  -d '{
    "items": [{
      "timestamp": "2026-02-14T12:00:00Z",
      "level": "info",
      "message": "Diagnostic message",
      "component": "test"
    }]
  }'
```

### Inject synthetic assistant message:

```bash
curl -X POST \
  "https://api.anthropic.com/v2/session_ingress/session/session_01ABC/events" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -H "X-Environment-Manager-Version: 1.0.0" \
  -d '{
    "events": [{
      "type": "assistant",
      "id": "550e8400-e29b-41d4-a716-446655440001",
      "data": {
        "content": [{
          "type": "text",
          "text": "I have completed the analysis..."
        }]
      }
    }]
  }'
```

---

## Implementation Notes

- Binary uses Go 1.25.6
- HTTP client implements automatic retries via `RetryableHTTPDo`
- OpenTelemetry trace propagation is configured but injection code not fully reconstructed.
  **As of Build ID `0b86a2a0` those spans are actually exported**: the binary
  gained a full OTLP **trace** pipeline (`otlptracehttp` -> `otlptrace.Exporter`
  -> `sdk/trace.TracerProvider`), the observability service grew a `Tracer()`
  method, and the backend's `OtlpEndpoints` call now returns three endpoints
  (logs, metrics, traces) instead of two. Evidence and addresses:
  <environment_manager/src/internal/o11y/otel_traces.go>. The propagation
  carrier used for injection is `FKPKJ5B0zZ.nfjR4Fsm`
  (`Get`/`Set`/`Keys` at `0x20156a0`/`0x20156e0`/`0x2015700`).
- All timestamps use format: `"2006-01-02T15:04:05.000000000Z07:00"`
- Session IDs are URL-escaped before inclusion in endpoint URLs
- Response bodies are always read for error reporting, even if HTTP call succeeds
