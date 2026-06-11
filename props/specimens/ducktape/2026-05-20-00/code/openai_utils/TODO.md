# openai_utils TODO

## llama-server `/v1/responses` incompatibilities

Potential issues when using our Pydantic types against llama-server's
`/v1/responses` endpoint (implemented in `server-common.cpp`,
`convert_responses_to_chatcmpl`).

Reference: <https://github.com/ggml-org/llama.cpp/blob/master/tools/server/server-common.cpp>

### High

- **`FunctionCallItem.arguments` can be `None`** (`model.py`).
  llama-server reads `arguments` as a required string field when building the
  chat-completion function-call object (`convert_responses_to_chatcmpl`,
  function_call branch). Sending `null` will likely cause a parse error.
  Fix: default to `""` or `"{}"` instead of `None`.

### Medium

- **`AssistantMessage.content` can be `None`** (`model.py`).
  llama-server iterates `content` as an array when processing assistant
  messages (`convert_responses_to_chatcmpl`, message branch ~line 1186).
  A `null` content will cause an iteration error.
  Fix: default to `[]` instead of `None`, or always populate before sending.

### Low

- **Extra fields on input items** (`model.py`).
  All models use `ConfigDict(extra="allow")`, so unknown fields are preserved
  on round-trip. `FunctionCallItem.id`, `FunctionCallItem.status`,
  `AssistantMessage.id`, and `ReasoningItem.content` are fields that
  llama-server does not expect. Strict JSON validation on the server side
  could reject them. In practice llama-server currently ignores unknown fields.

- **`ReasoningItem.summary` defaults to `[]`** (`model.py`).
  llama-server reads `summary` from reasoning items. An empty array is
  probably fine but is untested.
