local I = import '../../specimens/lib.libsonnet';

// iss-050: TOKEN_TABLE uses hardcoded mock data instead of file-based configuration

I.issueOneOccurrence(
  rationale=|||
    The TOKEN_TABLE in mcp_routing.py contains hardcoded mock data instead of using
    proper file-based configuration:

    ```python
    # Token table: token -> TokenInfo
    # In production, this would be a database lookup or external service
    TOKEN_TABLE: dict[str, TokenInfo] = {
        "human-token-123": HumanTokenInfo(role=TokenRole.HUMAN),
        "agent-token-abc": AgentTokenInfo(role=TokenRole.AGENT, agent_id=AgentID("agent-1")),
    }
    ```

    The comment "In production, this would be a database lookup or external service"
    indicates this is temporary/mock code where real implementation was intended.

    However, there's already a real implementation in the codebase: the TokenMapping
    class in adgn/src/adgn/agent/mcp_bridge/auth.py (lines 24-58) that:
    - Reads token mappings from a JSON file
    - Uses proper error handling (FileNotFoundError, ValueError)
    - Has a reload() method for updating mappings
    - Has a get_agent_id() method for lookups

    TokenMapping file format:
    ```json
    {
      "secret-token-123": "chatgpt-agent",
      "secret-token-456": "claude-agent"
    }
    ```

    The TOKEN_TABLE should use a similar approach, but extended to support both
    human and agent tokens with their respective metadata (role, agent_id).

    Fix:
    1. Create a similar file-based configuration class for TOKEN_TABLE
    2. Use Pydantic to parse JSON/YAML file into TokenInfo objects
    3. Remove the hardcoded mock data
    4. Update tests to use fixture files instead of patching the global

    Example file format (JSON):
    ```json
    {
      "human-token-123": {"role": "human"},
      "agent-token-abc": {"role": "agent", "agent_id": "agent-1"}
    }
    ```

    Or even better, unify with TokenMapping to have a single token configuration file
    that serves both purposes.
  |||,
  properties=['no-dead-code'],
  filesToRanges={
    'adgn/src/adgn/agent/server/mcp_routing.py': [
      [60, 63],  // Hardcoded TOKEN_TABLE with mock data
    ],
  },
  gap_note=|||
    This pattern deserves a property like "no-mock-code-in-production": when mock/stub
    implementations exist in production code with comments like "this would be X in production",
    they should be replaced with the real implementation. This is distinct from "no-dead-code"
    as it's not about unused code, but rather about temporary/placeholder code that should
    have been replaced before reaching production. It's also related to configuration
    management - hardcoded config that should come from files/environment.
  |||,
)
