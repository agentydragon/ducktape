# MCP Starter Template

MCP server template with essential practices and debugging tools:

- Simple hello world tools
- Content type examples - text, binary data, JSON responses
- STDIO and SSE modes
- Debug logging, request/response logging
- Claude Code-based acceptance test

### Example tools

- `greet` - Simple greeting function (returns "hello, {name}")
- `get_text_chunks` - Text chunking for streaming responses
- `generate_sample_image` - Binary content example (minimal PNG)

## Usage

Install package in development mode.

```bash
# STDIO mode (default); optionally add --debug
python -m adgn_mcp_starter

# SSE Mode (HTTP)
python -m adgn_mcp_starter --transport=sse --host=localhost --port=8000

# Run acceptance test
python claude_test.py
```

See `example_config.json` for example configuration file for MCP clients.

## Debugging & Troubleshooting

```bash
# Basic debug logging
python -m adgn_mcp_starter --debug

# Full MCP request/response logging
python -m adgn_mcp_starter --debug-mcp

# Claude MCP debugging
claude --debug --mcp-config example_config.json "test prompt"
```

### Common Issues

#### 1. Permission Errors

**Symptoms**: "Claude requested permissions" messages
**Solution**: Use `--permission-mode bypassPermissions` for testing

#### 2. Transport Issues

**Symptoms**: Connection refused, broken pipe
**Debug**: Check server logs, verify transport mode matches client

### Debugging Techniques

#### 1. Copy-Paste Debug

```bash
# Run server with debug output
python -m adgn_mcp_starter --debug 2> debug.log

# Copy interesting requests/responses from debug.log
# Paste into issue reports or debugging sessions
```

#### 2. Raw Request/Response Logging

```python
# In server.py, set:
DEBUG_LOGGING = True
# Or use --debug-mcp flag
```

#### 3. Claude Acceptance Test

```bash
# Run comprehensive test
python claude_test.py

# Check generated reports
ls /tmp/mcp_starter_test_*/
```

#### 5. MCP Inspector

```bash
# Install and run MCP Inspector
npx @modelcontextprotocol/inspector

# Point to your server:
# - Transport: stdio
# - Command: python -m adgn_mcp_starter --debug
# - Args: (leave empty)
```

## Links

- [MCP Protocol Specification](https://modelcontextprotocol.io/specification)
- [FastMCP Framework](https://github.com/jlowin/fastmcp)
- [MCP Inspector](https://github.com/modelcontextprotocol/inspector)
- [Claude Code MCP Integration](https://docs.anthropic.com/en/docs/claude-code/mcp)

## Testing

```bash
pytest
mypy .
python claude_test.py   # Acceptance test
```
