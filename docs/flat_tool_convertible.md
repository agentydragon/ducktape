# FlatTool → plain FastMCP: convertible tools

Tools using `.flat_model()` whose input models only have basic typed fields with
`Field(description=...)` and defaults. These could be plain FastMCP `@mcp.tool`
functions with `Annotated[T, "description"]` parameters instead.

## Convertible (16 tools)

| File                                       | Tool                       | Input model              |
| ------------------------------------------ | -------------------------- | ------------------------ |
| `git_commit_ai/agent_backend.py`           | `submit_commit_message`    | `CommitMessage`          |
| `mcp_infra/compositor/resources_server.py` | `list_resources`           | none (zero-arg)          |
| `mcp_infra/compositor/resources_server.py` | `list_templates`           | none (zero-arg)          |
| `mcp_infra/compositor/resources_server.py` | `list_subscriptions`       | none (zero-arg)          |
| `mcp_infra/compositor/resources_server.py` | `subscribe`                | `ResourcesSubscribeArgs` |
| `mcp_infra/compositor/resources_server.py` | `unsubscribe`              | `ResourcesSubscribeArgs` |
| `mcp_infra/compositor/resources_server.py` | `subscribe_list_changes`   | `ListSubscribeArgs`      |
| `mcp_infra/compositor/resources_server.py` | `unsubscribe_list_changes` | `ListSubscribeArgs`      |
| `mcp_infra/compositor/admin.py`            | `detach_server`            | `DetachServerArgs`       |
| `git_commit_ai/git_ro/server.py`           | `rev_parse`                | `RevParseInput`          |
| `mcp_infra/exec/seatbelt.py`               | `read_image`               | `ReadImageInput`         |
| `mcp_infra/exec/direct.py`                 | `read_image`               | `ReadImageInput`         |
| `mcp_infra/exec/bwrap.py`                  | `read_image`               | `ReadImageInput`         |
| `mcp_infra/exec/docker/server.py`          | `read_image`               | `ReadImageInput`         |
| `x/inop/prompt_feedback_mcp.py`            | `propose_prompt`           | `ProposePromptInput`     |
| `agent_core_testing/echo_server.py`        | `echo`                     | `EchoInput`              |

## Not convertible (18 tools)

Require FlatTool for: nested models, `Field(ge=, le=, min_length=, pattern=)`,
`ConfigDict(extra="forbid")`, custom methods, or enum schema generation.

| File                                       | Tool                       | Reason                                                        |
| ------------------------------------------ | -------------------------- | ------------------------------------------------------------- |
| `x/ember/mcp_tools.py`                     | `sleep_until_user_message` | `ConfigDict(extra="forbid")`                                  |
| `mcp_infra/compositor/resources_server.py` | `read`                     | `Field(ge=0)` on `start_offset`                               |
| `mcp_infra/compositor/resources_server.py` | `read_blocks`              | `Field(ge=0)` constraints                                     |
| `mcp_infra/compositor/admin.py`            | `attach_server`            | Nested models (`ServerSpec` union)                            |
| `git_commit_ai/git_ro/server.py`           | `status`                   | Nested model (`ListSlice`)                                    |
| `git_commit_ai/git_ro/server.py`           | `diff`                     | Nested models, `Field(ge=0, le=1000)`                         |
| `git_commit_ai/git_ro/server.py`           | `log`                      | Nested model (`TextSlice`)                                    |
| `git_commit_ai/git_ro/server.py`           | `show`                     | Nested models                                                 |
| `git_commit_ai/git_ro/server.py`           | `cat_file`                 | Nested model (`TextSlice`)                                    |
| `git_commit_ai/git_ro/server.py`           | `log_entries`              | `Field(ge=0)`, `Field(gt=0, le=1000)`                         |
| `git_commit_ai/git_ro/server.py`           | `ls_files`                 | Nested model (`ListSlice`)                                    |
| `git_commit_ai/git_ro/server.py`           | `branch_list`              | Nested model (`ListSlice`)                                    |
| `adgn/mcp/gitea_mirror/server.py`          | `trigger_mirror_sync`      | `ConfigDict(extra="forbid")`                                  |
| `adgn/mcp/gitea_mirror/server.py`          | `get_repo_info`            | `ConfigDict(extra="forbid")`                                  |
| `mcp_infra/exec/seatbelt.py`               | `sandbox_exec`             | Nested models, `Field(min_length=1)`, `ConfigDict`            |
| `mcp_infra/exec/direct.py`                 | `exec`                     | `Field(min_length=1)`, `Field(ge=0, le=100000)`, `ConfigDict` |
| `mcp_infra/exec/bwrap.py`                  | `exec`                     | `Field(ge=0, le=100000)`, `ConfigDict`                        |
| `mcp_infra/exec/docker/server.py`          | `exec`                     | Custom method, `Field(pattern=...)`, `ConfigDict`             |
