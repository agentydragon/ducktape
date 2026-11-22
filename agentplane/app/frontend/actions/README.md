# Action frontend

The `actions/` packages own Action display and operator workflows. `mcp/` owns the MCP server
settings view and its linkage client. The Settings dialog in `../settings/dialog.tsx` composes the
view. `actions/notifications` remains Action-specific notification presentation; Notification
Service inboxes live in `../notifications`.

Each renderer group has a screenshot suite under `//agentplane/app/frontend/actions`, for example
`bbr test //agentplane/app/frontend/actions:visual_github`. The suite mounts the production
sidebar, detail, and history cards with that group's renderer map. Register new groups in the App
catalog in `rendering/index.tsx` and in `visual_groups.bzl`. The catalog test checks group coverage,
and each browser suite checks that every registered Action has a named example.
