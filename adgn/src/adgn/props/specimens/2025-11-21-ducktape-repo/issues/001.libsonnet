local I = import '../../specimens/lib.libsonnet';

// iss-001: backend_key is stringly typed, should use TokenInfo as dict key

I.issueOneOccurrence(
  rationale=|||
    The `_backend_apps` dict uses string keys ("human" or f"agent:{agent_id}") when it
    should use TokenInfo objects directly as keys. This is stringly typed and error-prone.

    **Current code:**

    Line 80: `_backend_apps: dict[str, ASGIApp] = {}`

    Lines 93-108:
    ```python
    match token_info:
        case HumanTokenInfo():
            backend_key = "human"
            if backend_key not in self._backend_apps:
                self._backend_apps[backend_key] = self.agents_server.http_app()
            return self._backend_apps[backend_key]

        case AgentTokenInfo(agent_id=agent_id):
            backend_key = f"agent:{agent_id}"
            if backend_key not in self._backend_apps:
                container = await self.registry.ensure_live(agent_id, with_ui=False)
                compositor_app = container.running.compositor.http_app()
                self._backend_apps[backend_key] = compositor_app
            return self._backend_apps[backend_key]
    ```

    **Problems with stringly typed keys:**
    - **Type safety loss**: String keys don't capture the actual structure (TokenInfo)
    - **Error-prone**: Easy to make typo in string format (e.g., "agent:{agent_id}")
    - **Fragile**: If we add new TokenInfo types, must remember the string key format
    - **No IDE support**: Can't autocomplete or refactor string keys
    - **Duplication**: The match already discriminates on TokenInfo, then creates redundant string
    - **Inconsistent**: token_info is strongly typed, but backend_key throws that away

    **Correct approach:**

    Use TokenInfo directly as the dict key:

    ```python
    # Line 80:
    _backend_apps: dict[TokenInfo, ASGIApp] = {}

    # Lines 93-108:
    match token_info:
        case HumanTokenInfo():
            if token_info not in self._backend_apps:
                self._backend_apps[token_info] = self.agents_server.http_app()
            return self._backend_apps[token_info]

        case AgentTokenInfo():
            if token_info not in self._backend_apps:
                container = await self.registry.ensure_live(token_info.agent_id, with_ui=False)
                compositor_app = container.running.compositor.http_app()
                self._backend_apps[token_info] = compositor_app
            return self._backend_apps[token_info]
    ```

    **Why TokenInfo as key works:**
    - TokenInfo types are Pydantic BaseModels
    - Pydantic models with `model_config = ConfigDict(frozen=True)` are hashable
    - Need to make HumanTokenInfo and AgentTokenInfo frozen (add `frozen=True` to config)
    - Then TokenInfo instances can be dict keys
    - Proper type safety: dict key type matches the actual discriminator

    **Alternative if not making models frozen:**
    Create a proper key type or use a different caching strategy, but frozen models are
    the cleanest solution.

    **Required changes:**
    1. Add `model_config = ConfigDict(frozen=True)` to HumanTokenInfo (line 38-41)
    2. Add `model_config = ConfigDict(frozen=True)` to AgentTokenInfo (line 44-48)
    3. Change `_backend_apps: dict[str, ASGIApp]` to `dict[TokenInfo, ASGIApp]` (line 80)
    4. Replace backend_key string construction with direct token_info usage (lines 95-108)
    5. Remove backend_key variable entirely

    **Benefits:**
    - Type-safe: can't accidentally use wrong key
    - Cleaner: no string construction
    - Safer: compiler/type checker catches errors
    - More maintainable: adding new TokenInfo types is straightforward
  |||,
  properties=['type-safety', 'stringly-typed', 'api-design', 'maintainability'],
  filesToRanges={
    'adgn/src/adgn/agent/server/mcp_routing.py': [
      [80, 80],   // _backend_apps dict with string keys
      [93, 108],  // _get_backend_app method creating string keys
      [38, 41],   // HumanTokenInfo - needs frozen=True
      [44, 48],   // AgentTokenInfo - needs frozen=True
    ],
  },
)
