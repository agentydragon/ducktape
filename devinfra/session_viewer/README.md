# Local session viewer

A workstation browser for locally captured Claude Code and Codex CLI sessions. The page reads a local API and never pairs with a Claude account or requests cloud sessions.

The frontend is `//devinfra/session_viewer/frontend:bundle`; its Bazel output is the portable `dist/` directory (`index.html`, `main.js`, and source maps) for the local HTTP server to mount.

The read API serves machine and session pages, distinct content versions, and ordered physical JSONL lines. Each event includes its parsed value when valid, a parse error when invalid, and the original line bytes in base64. The UI presents user and assistant text, tool calls, timestamps, machine names, and version times, while keeping every line available in an expandable raw view.

Claude subagent transcripts are indexed with their parent when the importer can infer it from the local `subagents/` path; the viewer labels inferred links and uncaptured parent placeholders. Codex transcripts under `sessions/` and `archived_sessions/` are listed, but Codex parent-child relationships are not inferred yet.
