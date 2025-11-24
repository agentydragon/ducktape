# Provider Architecture Migration TODO

## Status

✅ **Completed:**
- Created truly provider-agnostic types in `adgn/llm/types.py`
- Created `LLMProvider` protocol in `adgn/llm/provider.py`
- Implemented clean `AnthropicProvider` using native Messages API
- Implemented clean `OpenAIProvider` using native Responses API
- Fixed ResponseUsage validation for Anthropic adapter
- Renamed `OpenAIModelProto` → `LLMProvider` with backward compat

✅ **Tested:**
- New Anthropic provider works correctly (31 tokens simple, 407 tokens with tools)
- Provider-specific retry logic implemented in each provider

## Remaining Work

### 1. Migrate Agent Code to New Provider Interface

**Current state:**
- Agent code still uses old `openai_utils` types (ResponsesRequest/ResponsesResult)
- `build_client()` still returns old adapter/wrapper types
- Two parallel systems exist: old (openai_utils) and new (adgn/llm)

**Migration steps:**

1. **Update `build_client()` factory** (`adgn/openai_utils/client_factory.py`)
   - Return new `AnthropicProvider` / `OpenAIProvider` instead of adapters
   - Keep backward compatibility during transition

2. **Create adapter layer** (temporary)
   - Build adapter that translates old ResponsesRequest → new CompletionRequest
   - Build adapter that translates new CompletionResult → old ResponsesResult
   - This allows gradual migration of consuming code

3. **Migrate agent code** (`adgn/agent/agent.py`)
   - Update to use new `adgn/llm` types instead of `openai_utils` types
   - Update MiniCodex to work with `CompletionRequest`/`CompletionResult`

4. **Migrate other consumers:**
   - `adgn/inop/grading/grader.py`
   - `adgn/inop/prompting/summarizer.py`
   - `adgn/props/eval_harness.py`
   - `adgn/llm/llm_edit.py`

5. **Deprecate old types:**
   - Mark ResponsesRequest/ResponsesResult as deprecated
   - Add migration guide comments
   - Eventually remove after all code migrated

### 2. Handle Reasoning Blocks and Advanced Features

**Issue:** New agnostic types don't yet support:
- Reasoning blocks (OpenAI o1 models)
- Multimodal content (images, etc.)
- Streaming responses

**Options:**
1. Add to agnostic types with graceful degradation (providers that don't support ignore)
2. Use provider-specific extensions via `extra="allow"`
3. Create specialized protocols for advanced features

**Recommendation:** Start with option 1 for reasoning blocks since they're already in use.

### 3. Specimen Isolation Testing

**Current blocker:** Environment restrictions prevent Docker/Podman installation
- Network: 403 errors from GitHub/Docker repos
- No sudo access
- /tmp permission issues

**Next steps:**
- Test in less restricted environment (local dev machine, CI/CD)
- Verify specimens work with new Anthropic provider
- Get actual metrics from specimen runs with Haiku

**Alternative isolation options to explore:**
- Python venv isolation (lighter weight, already available)
- Bubblewrap sandboxing (if available)
- Modify specimen runner to work without Docker (use subprocess isolation)

### 4. Documentation

**Needed:**
- Architecture decision record (ADR) explaining the two-tier design
- Migration guide for consumers of old types
- Examples showing how to use new provider system
- Document provider-specific features and limitations

### 5. Performance and Optimization

**Future improvements:**
- HTTP connection pooling across providers
- Caching layer for repeated requests
- Metrics/observability for provider performance
- Provider fallback/retry strategies

## Architecture Design Decisions

### Why Two Systems Exist

**Old system** (`openai_utils`):
- Based on OpenAI Responses API types (ResponsesRequest/ResponsesResult)
- Used throughout existing agent/grading code
- AnthropicAdapter forces Anthropic into OpenAI format

**New system** (`adgn/llm`):
- Truly provider-agnostic types (CompletionRequest/CompletionResult)
- Each provider uses native API format internally
- Clean separation of concerns

### Migration Strategy

**Gradual, not big-bang:**
1. New providers exist alongside old adapters
2. Adapter layer bridges old ↔ new during transition
3. Migrate consumers one at a time
4. Remove old system once migration complete

This minimizes risk and allows incremental testing.

## Questions for Discussion

1. **Reasoning blocks:** Should they be in agnostic types or provider-specific?
2. **Streaming:** Add to protocol or separate interface?
3. **Multimodal:** How to represent images/audio in agnostic format?
4. **Specimen isolation:** Acceptable to skip Docker requirement for now?

## Related Files

**New provider system:**
- `adgn/src/adgn/llm/types.py` - Agnostic types
- `adgn/src/adgn/llm/provider.py` - Protocol definition
- `adgn/src/adgn/llm/providers/anthropic.py` - Anthropic implementation
- `adgn/src/adgn/llm/providers/openai.py` - OpenAI implementation

**Old system (to be deprecated):**
- `adgn/src/adgn/openai_utils/model.py` - ResponsesRequest/ResponsesResult
- `adgn/src/adgn/openai_utils/anthropic_adapter.py` - Old Anthropic adapter
- `adgn/src/adgn/openai_utils/retry.py` - Old retry wrapper
- `adgn/src/adgn/openai_utils/client_factory.py` - Factory (needs update)

**Consumers to migrate:**
- `adgn/src/adgn/agent/agent.py` - MiniCodex agent
- `adgn/src/adgn/inop/` - INOP grading/optimization
- `adgn/src/adgn/props/` - Properties project
