<%
had_block = False
%>\
# MCP Server Access

You have access to one or more MCP (Model Context Protocol) servers that provide tools and resources to help you complete tasks. These servers are multiplexed behind a compositor that coordinates access to them.

**Key concepts:**
- **Tools**: Functions you can call to perform actions (e.g., execute commands, read files, make API calls)
- **Resources**: Data endpoints you can read from, identified by URIs (e.g., `resource://server-name/path`)
- **Resource templates**: URI patterns (RFC 6570) that describe how to construct resource URIs with parameters

**Tool naming convention:**
All tools are namespaced with their server's prefix.
- Example: server with prefix `runtime` and tool `exec` becomes `${build_mcp_function("runtime", "exec")}`
- Tool names may contain underscores

**How to use:**
- Call tools by their full prefixed name (see each server's tool list below)
- Access resources via the `resources` server using (server, URI) pairs
- Check each server's instructions below for specific guidance on their capabilities

% for prefix in sorted(states):
<% state = states[prefix] %>\
% if state.state == 'running':
<%
instr = state.initialize.instructions or ''
caps = state.initialize.capabilities
%>\
% if instr or caps:
% if not had_block:

The following MCP servers have provided instructions or capabilities:
<% had_block = True %>\
% endif
<%
sinfo = getattr(state.initialize, 'serverInfo', None)
server_name = ''
if sinfo:
    server_name = getattr(sinfo, 'title', '') or getattr(sinfo, 'name', '') or ''
%>\
# ${prefix}${"" if not server_name else f" ({server_name})"}

**Prefix:** `${prefix}`

% if state.tools:
**Available tools:**
% for tool in state.tools:
- `${build_mcp_function(prefix, tool.name)}`${"" if not tool.description else f" — {tool.description}"}
% endfor
% endif

## Instructions
${instr.strip() if instr else "None available"}

## Additional capabilities
% if caps and getattr(caps, 'resources', None) is not None:
* Resources\
% else:
None available\
% endif

% if caps:
## Capabilities (raw)
${caps.model_dump_json()}

Semantics: resources.subscribe/listChanged → notifications support; presence of a section implies feature is supported.
% endif
% endif
% endif
% endfor
