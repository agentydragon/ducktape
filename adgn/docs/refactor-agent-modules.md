# Agent Module Refactoring Proposal

## Overview

Split `adgn/agent` into two packages:
- **`adgn/agent`** - Lightweight core for simple agent use cases (git-commit-ai, props agents, scripts)
- **`adgn/agent_server`** - Full-featured server with policies, approvals, multi-agent orchestration, and web UI

## Current Structure

```
adgn/src/adgn/agent/
├── agent.py                    # Core agent loop (MiniCodex)
├── handler.py                  # BaseHandler interface
├── events.py                   # Event types (UserText, ToolCall, etc.)
├── types.py                    # AgentID, etc.
├── loop_control.py             # Abort, NoAction, InjectItems, etc.
├── tool_schemas.py             # Tool schema utilities
├── display/                    # Rich CLI display
├── compaction_handler.py
├── recording_handler.py
├── transcript_handler.py
├── notifications/              # MCP notifications handler
│
├── server/                     # FastAPI app + UI state
│   ├── app.py
│   ├── bus.py                  # ServerBus (misnamed)
│   ├── state.py                # UiState, DisplayItem
│   ├── reducer.py
│   ├── history.py
│   ├── protocol.py
│   ├── runtime.py              # UiEventHandler, AgentSession
│   ├── mcp_routing.py
│   ├── rendering.py
│   ├── system_message.py
│   └── status_shared.py
│
├── runtime/                    # Docker container infrastructure
│   ├── container.py            # AgentContainer (god object)
│   ├── registry.py             # AgentRegistry
│   ├── handlers.py
│   ├── auto_attach.py
│   └── images.py
│
├── mcp_bridge/                 # Multi-agent orchestration (misnamed)
│   ├── registry.py             # InfrastructureRegistry (overlaps with runtime/registry.py)
│   ├── agents.py
│   ├── compositor_factory.py
│   └── auth.py
│
├── policies/                   # Policy types + programs
│   ├── policy_types.py
│   ├── scaffold.py
│   ├── default_policy.py
│   ├── approve_all.py
│   ├── loader.py
│   └── presets.py
│
├── policy_eval/                # Container-based policy execution
│   ├── container.py
│   ├── runner.py
│   ├── shim.py
│   └── constants.py
│
├── persist/                    # Persistence layer
│   ├── __init__.py
│   ├── sqlite.py
│   ├── events.py
│   └── handler.py
│
├── models/
│   ├── policy_error.py
│   └── proposal_status.py
│
├── approvals.py
├── bootstrap.py
├── presets.py
├── cli.py
├── db_event_handler.py
├── matrix_bot.py
│
├── web/                        # Svelte frontend source
├── static/                     # Built frontend assets
└── templates/                  # Jinja templates
```

## Proposed Structure

### `adgn/agent/` (Core)

Minimal, no server dependencies. Usable standalone.

```
agent/
├── agent.py                    # MiniCodex
├── handler.py                  # BaseHandler, SequenceHandler, AbortIf
├── events.py                   # UserText, AssistantText, ToolCall, ToolCallOutput, Response
├── types.py                    # AgentID
├── loop_control.py             # Abort, NoAction, InjectItems, Compact, ToolPolicy variants
├── tool_schemas.py
├── compaction_handler.py
├── recording_handler.py
├── transcript_handler.py
├── notifications/              # Lightweight, only depends on handler + loop_control
│   ├── handler.py
│   └── types.py
└── display/                    # Rich CLI display (optional)
    ├── __init__.py
    ├── agent_progress.py
    ├── event_renderer.py
    └── rich_display.py
```

### `adgn/agent_server/` (Server)

Full-featured server backend.

```
agent_server/
├── app.py                      # FastAPI factory, startup/shutdown
├── cli.py                      # adgn-mini-codex entrypoint
│
├── container/                  # Agent container lifecycle
│   ├── actor.py                # Actor pattern (mailbox, task lifecycle)
│   ├── container.py            # AgentContainer (composed, not god object)
│   ├── mcp_setup.py            # MCP infrastructure setup
│   ├── policy_setup.py         # Policy infrastructure setup
│   ├── agent_setup.py          # Agent runtime wiring
│   ├── handlers.py             # build_handlers()
│   ├── auto_attach.py
│   └── images.py
│
├── orchestration/              # Multi-agent management (was mcp_bridge/)
│   ├── registry.py             # AgentOrchestrator (consolidate both registries)
│   ├── agents_server.py        # MCP server for agent CRUD
│   ├── compositor_factory.py
│   └── auth.py                 # TokensConfig, TokenRoutingASGI
│
├── policies/                   # Complete policy subsystem
│   ├── types.py                # ApprovalDecision, PolicyRequest, PolicyResponse
│   ├── approvals.py            # ApprovalRequest, WellKnownTools, load_default_policy_source()
│   ├── scaffold.py             # run(), run_with_tests()
│   ├── loader.py               # load_policy_text()
│   ├── programs/               # Built-in policy programs
│   │   ├── default.py
│   │   └── approve_all.py
│   └── eval/                   # Container-based execution
│       ├── container.py        # ContainerPolicyEvaluator
│       ├── runner.py           # run_policy_source()
│       ├── shim.py             # In-container shim
│       └── constants.py
│
├── persist/                    # Persistence layer
│   ├── __init__.py             # Persistence protocol, models
│   ├── sqlite.py               # SQLitePersistence
│   ├── events.py               # EventRecord
│   └── handler.py              # RunPersistenceHandler
│
├── ui/                         # OPTIONAL: Self-contained UI component
│   ├── bus.py                  # UiBus (was ServerBus), UiMessage, UiEndTurn
│   ├── state.py                # UiState, DisplayItem, ToolItem, etc.
│   ├── reducer.py              # reduce_ui_state()
│   ├── history.py              # fold_events_to_ui_state()
│   ├── handler.py              # UiEventHandler
│   ├── session.py              # AgentSession
│   ├── facet.py                # UiFacet dataclass
│   └── mode_handler.py         # ServerModeHandler
│
├── protocol/                   # Wire types
│   ├── messages.py             # ServerMessage, TranscriptItem, FunctionCallOutput
│   ├── snapshot.py             # Snapshot, SessionState, ApprovalPolicyInfo
│   └── status.py               # AgentStatusCore, RunPhase, AgentLifecycle
│
├── middleware/
│   └── mcp_routing.py          # MCPRoutingMiddleware
│
├── models/                     # Shared models
│   ├── policy_error.py         # PolicyError, PolicyTestsSummary
│   └── proposal_status.py      # ProposalStatus
│
├── presets.py                  # AgentPreset, discover_presets(), create_agent_from_preset()
├── bootstrap.py                # TypedBootstrapBuilder
├── rendering.py                # render_compositor_instructions()
├── system_message.py           # get_ui_system_message()
├── db_event_handler.py
├── matrix_bot.py
│
├── web/                        # Svelte frontend source
├── static/                     # Built frontend assets
└── templates/                  # Jinja templates
```

## Key Changes

### Renames

| Old | New | Reason |
|-----|-----|--------|
| `server/runtime.py` | `ui/session.py` + `ui/handler.py` | Disambiguate from `runtime/` directory |
| `server/bus.py` → `ServerBus` | `ui/bus.py` → `UiBus` | It's UI-specific, not a general server bus |
| `mcp_bridge/` | `orchestration/` | It's multi-agent orchestration, not a "bridge" |
| `runtime/` | `container/` | Clearer purpose |
| `server/status_shared.py` | `protocol/status.py` | Group with other protocol types |

### Consolidations

| Items | Into | Reason |
|-------|------|--------|
| `AgentRegistry` + `InfrastructureRegistry` | `AgentOrchestrator` | Overlapping responsibility, Phase 5 migration artifact |
| `policies/` + `policy_eval/` + `approvals.py` | `policies/` | Scattered policy code |
| `server/state.py` + `server/reducer.py` + `server/history.py` | `ui/` | All UI-specific state management |

### Splits

| From | Into | Reason |
|------|------|--------|
| `AgentContainer` (647 lines) | `actor.py`, `mcp_setup.py`, `policy_setup.py`, `agent_setup.py` | God object with too many responsibilities |
| `server/runtime.py` | `ui/handler.py` (UiEventHandler), `ui/session.py` (AgentSession) | Mixed concerns |

### UI as Optional Component

The `ui/` module is:
- **Self-contained**: All UI state management in one place
- **Optional**: Not imported when `with_ui=False`
- **Pluggable**: Container conditionally creates UiFacet

Components in `ui/`:
- `UiBus` - Message queue for UI ↔ handler communication
- `UiState` - Immutable display state
- `reduce_ui_state()` - Redux-style reducer
- `UiEventHandler` - BaseHandler that forwards events to UI
- `AgentSession` - Run lifecycle for UI consumption
- `fold_events_to_ui_state()` - Replay persisted events

## Test Structure

```
tests/
├── agent/                      # Core agent tests only
│   └── ...
├── agent_server/               # Server-specific tests
│   ├── container/
│   ├── orchestration/
│   ├── policies/
│   ├── persist/
│   ├── ui/
│   ├── e2e/
│   └── ...
└── mcp_bridge/                 # Move under agent_server/ or keep separate
```

## pyproject.toml Updates

```toml
[project.scripts]
adgn-mini-codex = "adgn.agent_server.cli:main"  # was adgn.agent.cli:main

[tool.setuptools.package-data]
"adgn.agent_server" = [
  "static/*",
  "static/**/*",
]
"adgn.agent_server.templates" = [
  "*.j2",
]
```

## Migration Notes

1. **Import rewiring**: ~50+ files need import path updates
2. **Circular dependencies**: Watch for cycles between `container/` and `ui/`
3. **Test fixtures**: Many tests use agent fixtures that will need path updates
4. **Phase cleanup**: Remove "Phase N" legacy code after migration
