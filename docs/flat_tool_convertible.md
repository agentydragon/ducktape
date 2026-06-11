# FlatTool → plain FastMCP: convertible tools

Tools using `.flat_model()` whose input models only have basic typed fields with
`Field(description=...)` and defaults. These could be plain FastMCP `@mcp.tool`
functions with `Annotated[T, "description"]` parameters instead.

## Convertible (28 tools)

| File                                         | Tool                       | Input model              |
| -------------------------------------------- | -------------------------- | ------------------------ |
| `editor_agent/host/submit_server.py`         | `submit_success`           | `SubmitSuccessInput`     |
| `editor_agent/host/submit_server.py`         | `submit_failure`           | `SubmitFailureInput`     |
| `git_commit_ai/agent_backend.py`             | `submit_commit_message`    | `CommitMessage`          |
| `mcp_infra/compositor/resources_server.py`   | `list_resources`           | none (zero-arg)          |
| `mcp_infra/compositor/resources_server.py`   | `list_templates`           | none (zero-arg)          |
| `mcp_infra/compositor/resources_server.py`   | `list_subscriptions`       | none (zero-arg)          |
| `mcp_infra/compositor/resources_server.py`   | `subscribe`                | `ResourcesSubscribeArgs` |
| `mcp_infra/compositor/resources_server.py`   | `unsubscribe`              | `ResourcesSubscribeArgs` |
| `mcp_infra/compositor/resources_server.py`   | `subscribe_list_changes`   | `ListSubscribeArgs`      |
| `mcp_infra/compositor/resources_server.py`   | `unsubscribe_list_changes` | `ListSubscribeArgs`      |
| `mcp_infra/compositor/admin.py`              | `detach_server`            | `DetachServerArgs`       |
| `git_commit_ai/git_ro/server.py`             | `rev_parse`                | `RevParseInput`          |
| `mcp_infra/exec/seatbelt.py`                 | `read_image`               | `ReadImageInput`         |
| `mcp_infra/exec/direct.py`                   | `read_image`               | `ReadImageInput`         |
| `mcp_infra/exec/bwrap.py`                    | `read_image`               | `ReadImageInput`         |
| `mcp_infra/exec/docker/server.py`            | `read_image`               | `ReadImageInput`         |
| `agent_server/mcp_bridge/agents.py`          | `create_agent`             | `CreateAgentInput`       |
| `agent_server/mcp_bridge/agents.py`          | `delete_agent`             | `DeleteAgentInput`       |
| `agent_server/mcp_bridge/agents.py`          | `boot_agent`               | `BootAgentInput`         |
| `agent_server/runtime/container.py`          | `send_prompt`              | `SendPromptInput`        |
| `agent_server/runtime/container.py`          | `abort`                    | none (zero-arg)          |
| `agent_server/matrix_bot.py`                 | `do_yield`                 | none (zero-arg)          |
| `inop/mcp/prompt_feedback_server.py`         | `propose_prompt`           | `ProposePromptInput`     |
| `agent_server/mcp/matrix/server.py`          | `send`                     | `SendMessageInput`       |
| `agent_server/mcp/matrix/server.py`          | `drain_new_messages`       | none (zero-arg)          |
| `agent_server/mcp/matrix/server.py`          | `do_yield`                 | `YieldInput`             |
| `agent_server/mcp/matrix/control.py`         | `do_yield`                 | none (zero-arg)          |
| `agent_server/mcp/approval_policy/engine.py` | `create_proposal`          | `CreateProposalArgs`     |
| `agent_server/mcp/approval_policy/engine.py` | `withdraw_proposal`        | `WithdrawProposalArgs`   |
| `agent_server/mcp/approval_policy/engine.py` | `set_policy`               | `SetPolicyTextArgs`      |
| `agent_server/mcp/loop/server.py`            | `yield_turn`               | `YieldTurnArgs` (empty)  |
| `agent_server/mcp/chat/server.py`            | `post`                     | `PostInput`              |
| `agent_core_testing/echo_server.py`          | `echo`                     | `EchoInput`              |

## Not convertible (29 tools)

Require FlatTool for: nested models, `Field(ge=, le=, min_length=, pattern=)`,
`ConfigDict(extra="forbid")`, custom methods, or enum schema generation.

| File                                         | Tool                       | Reason                                                        |
| -------------------------------------------- | -------------------------- | ------------------------------------------------------------- |
| `ember/mcp_tools.py`                         | `sleep_until_user_message` | `ConfigDict(extra="forbid")`                                  |
| `mcp_infra/compositor/resources_server.py`   | `read`                     | `Field(ge=0)` on `start_offset`                               |
| `mcp_infra/compositor/resources_server.py`   | `read_blocks`              | `Field(ge=0)` constraints                                     |
| `mcp_infra/compositor/admin.py`              | `attach_server`            | Nested models (`ServerSpec` union)                            |
| `git_commit_ai/git_ro/server.py`             | `status`                   | Nested model (`ListSlice`)                                    |
| `git_commit_ai/git_ro/server.py`             | `diff`                     | Nested models, `Field(ge=0, le=1000)`                         |
| `git_commit_ai/git_ro/server.py`             | `log`                      | Nested model (`TextSlice`)                                    |
| `git_commit_ai/git_ro/server.py`             | `show`                     | Nested models                                                 |
| `git_commit_ai/git_ro/server.py`             | `cat_file`                 | Nested model (`TextSlice`)                                    |
| `git_commit_ai/git_ro/server.py`             | `log_entries`              | `Field(ge=0)`, `Field(gt=0, le=1000)`                         |
| `git_commit_ai/git_ro/server.py`             | `ls_files`                 | Nested model (`ListSlice`)                                    |
| `git_commit_ai/git_ro/server.py`             | `branch_list`              | Nested model (`ListSlice`)                                    |
| `adgn/mcp/gitea_mirror/server.py`            | `trigger_mirror_sync`      | `ConfigDict(extra="forbid")`                                  |
| `adgn/mcp/gitea_mirror/server.py`            | `get_repo_info`            | `ConfigDict(extra="forbid")`                                  |
| `mcp_infra/exec/seatbelt.py`                 | `sandbox_exec`             | Nested models, `Field(min_length=1)`, `ConfigDict`            |
| `mcp_infra/exec/direct.py`                   | `exec`                     | `Field(min_length=1)`, `Field(ge=0, le=100000)`, `ConfigDict` |
| `mcp_infra/exec/bwrap.py`                    | `exec`                     | `Field(ge=0, le=100000)`, `ConfigDict`                        |
| `mcp_infra/exec/docker/server.py`            | `exec`                     | Custom method, `Field(pattern=...)`, `ConfigDict`             |
| `agent_server/mcp/approval_policy/engine.py` | `evaluate_policy`          | `ConfigDict(extra="forbid")`                                  |
| `agent_server/mcp/approval_policy/engine.py` | `decide_call`              | `StrEnum` field (`CallDecision`)                              |
| `agent_server/mcp/approval_policy/engine.py` | `decide_proposal`          | `StrEnum` field (`ProposalDecision`)                          |
| `agent_server/mcp/chat/server.py`            | `read_pending_messages`    | `Field(ge=1, le=1000)`                                        |
| `agent_server/mcp/ui/server.py`              | `send_message`             | `ConfigDict(extra="forbid")`, `Literal` default               |
| `agent_server/mcp/ui/server.py`              | `end_turn`                 | `ConfigDict(extra="forbid")`                                  |
