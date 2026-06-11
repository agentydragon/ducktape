# Markdown Formatting Guidelines

Semantic formatting rules that automated tools (Prettier, markdownlint) cannot enforce.

## Inline Code (backticks)

Use inline code for:

- **Identifiers**: Variable names, function names, class names, field names
  - `call_id`, `__init__`, `MyClass`, `tool_name`
- **CLI commands and flags**: Command names, arguments, options (in prose)
  - `git status`, `--verbose`, `-n`, `ansible-playbook --syntax-check`
- **File paths and globs**: When referenced in prose (not as clickable links)
  - `src/utils.py`, `*.gen.*`, `test_*.py`
- **API paths and URL fragments**: Endpoint paths, query parameters
  - `/v1/responses`, `/api/v1/user`
- **Parameter names and values**: Configuration keys, enum values, literals
  - `max_output_tokens`, `reasoning_effort: high`, `state=open`
- **Technical literals**: Specific strings, patterns, regexes
  - `refs/for/*`, `deny_*`, `PDNS_API_KEY`
- **Environment variables**: `OPENAI_API_KEY`, `HTTP_PROXY`, `PG*` (patterns too)
- **Tool/function references in prose**: `functions.Read`, `mcp__github__search_code`
- **Database/SQL identifiers**: Table names (`reported_issues`), column names, SQL keywords in headers (`SELECT`, `INSERT`)

**Common mistakes**:

- Forgetting backticks lets Prettier mangle underscores: `__init__` → `**init**`, `_method` → `\_method`
- Underscores in test names: `test_sandboxer_compose.py` → `test*sandboxer_compose.py` (use inline code!)
- Glob patterns with underscores: `WP_*` → `WP*\*` (protect with backticks)
- Using inline code inside link targets: `[text](\`path\`)` — don't do this

## Links

Use angle brackets for local file references without custom text:

```markdown
See <nginx/gitea_pr_gate.conf> for details.
```

Use standard markdown links for custom text:

```markdown
See the [config file](nginx/gitea_pr_gate.conf) for details.
```

**Do NOT** duplicate paths unnecessarily:

```markdown
# Bad - duplicates path

See [nginx/gitea_pr_gate.conf](nginx/gitea_pr_gate.conf)

# Good - angle brackets for same effect

See <nginx/gitea_pr_gate.conf>
```

## Code Blocks

Use fenced code blocks for:

- **Multi-line code examples**: Actual code, config files, command sequences
- **Structured output**: JSON, YAML, command output
- **ASCII diagrams**: Use `text` as the language specifier

**Always specify a language** (markdownlint MD040 enforces this):

```python
def example():
    pass
```

```bash
git status
git commit -m "message"
```

```text
┌─────────────┐
│  Diagram    │
└─────────────┘
```

**Common mistakes**:

- Mixing different content types in one block (e.g., function calls with different tools)
  - Split into separate blocks with descriptive subheadings
- Indenting sub-sections within a code block instead of using separate fenced blocks
- Forgetting language specifier (empty ` ``` `)
- Using prose headers inside code blocks (e.g., `Responses API (/v1/responses)` as a line in a code block)
  - Use markdown headings outside the block instead
- Raw JSON without code blocks — always wrap structured data in fenced blocks with `json` language

## When to Split Code Blocks

If a code block contains logically separate sections (different tools, different APIs, different categories), split into multiple blocks with subheadings.

**Bad** - mixing categories and using prose headers inside code:

````markdown
```
functions.Read (local files)
  file_path: /path/to/file1.js
  file_path: /path/to/file2.js

functions.mcp__github__get_file_contents (GitHub API)
  { owner: org, repo: repo, path: "src/" }
```
````

**Good** - separate blocks with markdown headings:

````markdown
### Local File Reads

```text
file_path: /path/to/file1.js
file_path: /path/to/file2.js
```

### GitHub API Calls

```text
{ owner: org, repo: repo, path: "src/" }
```
````

## API Documentation Pattern

When documenting API parameters, use markdown structure outside code blocks:

**Bad** - prose section headers inside a code block:

````markdown
```
Responses API (/v1/responses)
  - max_output_tokens
  - reasoning { effort: ... }

Chat Completions API (/v1/chat/completions)
  - max_completion_tokens
```
````

**Good** - markdown headings with inline code for technical terms:

```markdown
### Responses API (`/v1/responses`)

- `max_output_tokens`
- `reasoning` - `{ effort: minimal|low|medium|high }`

### Chat Completions API (`/v1/chat/completions`)

- `max_completion_tokens`
```

## Model/Schema Documentation

When documenting data structures with fields:

**Bad** - verbose, hard to scan:

```markdown
- `ToolGroup`
  - `id`: unique identifier
  - `ts`: timestamp
  - `tool`: tool name
  - `call_id`: call identifier
```

**Good** - compact inline:

```markdown
- `ToolGroup` - `{id, ts, tool, call_id, cmd?, approvals, stdout, stderr, exit_code}`
```

Use `?` suffix for optional fields. This pattern works well for Pydantic models, TypedDicts, and similar structures.

## Summary Table

| Content Type              | Format              | Example                    |
| ------------------------- | ------------------- | -------------------------- |
| Variable/function name    | Inline code         | `call_id`                  |
| CLI command               | Inline code         | `git status`               |
| File path in prose        | Inline code         | `src/utils.py`             |
| Local file link           | Angle brackets      | `<path/to/file.md>`        |
| Local file link with text | Standard link       | `[guide](path/to/file.md)` |
| API endpoint              | Inline code         | `/v1/responses`            |
| Parameter name            | Inline code         | `max_tokens`               |
| Multi-line code           | Fenced block        | ` ```python ... ``` `      |
| ASCII diagram             | Fenced block (text) | ` ```text ... ``` `        |
| Environment variable      | Inline code         | `OPENAI_API_KEY`           |
| Glob/pattern              | Inline code         | `*.gen.*`                  |
| Database table name       | Inline code         | `reported_issues`          |
| SQL keyword (in headers)  | Inline code         | `SELECT`, `INSERT`         |
