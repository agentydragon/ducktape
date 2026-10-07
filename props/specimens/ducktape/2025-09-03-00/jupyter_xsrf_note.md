# Note: Jupyter MCP XSRF behavior

Summary

- The sandboxed Jupyter server used by the MCP disables XSRF (c.ServerApp.disable_check_xsrf = True) while supplying a
  token and binding to 127.0.0.1. This weakens protections even with a token. We keep this as a tracked note because the
  Jupyter MCP we use may not support XSRF (needs verification).

Code references (specimen paths)

- llm/adgn_llm/src/adgn_llm/mcp/sandboxed_jupyter_mcp/jupyter_sandbox_compose.py:55
  - "c.ServerApp.disable_check_xsrf = True"
- llm/adgn_llm/src/adgn_llm/mcp/sandboxed_jupyter_mcp/wrapper.py:82–85
  - "--ServerApp.token {tq}", "--ServerApp.password ''", "--ServerApp.disable_check_xsrf True"
- llm/adgn_llm/src/adgn_llm/mcp/sandboxed_jupyter_mcp/wrapper.py:234–239
  - "--ServerApp.token", token, "--ServerApp.password", "", "--ServerApp.disable_check_xsrf", "True"
- llm/adgn_llm/src/adgn_llm/mcp/sandboxed_jupyter_mcp/wrapper.py:356
  - "c.ServerApp.disable_check_xsrf = True"
- llm/adgn_llm/src/adgn_llm/mcp/sandboxed_jupyter_mcp/jupyter_mcp_launch.py:54–55
  - "--ServerApp.disable_check_xsrf", "True"

Rationale / current stance

- GitHub MCP/Jupyter runtime is local-only with token auth, but disabling XSRF reduces defense-in-depth. We will not
  flag this as a violation right now due to possible MCP server limitations, but we should revisit.

Action items

- Verify whether the Jupyter MCP and its downstream clients support XSRF properly. If supported, remove all
  disable_check_xsrf settings and rely on token+XSRF.
- Keep token required, bind to 127.0.0.1, and keep password empty only with token present.
- If XSRF must remain disabled for technical reasons, document the constraint clearly and gate behind a dev/test flag.
