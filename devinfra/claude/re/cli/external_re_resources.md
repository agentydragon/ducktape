# External Claude Code CLI Reverse Engineering Resources

Last updated: 2026-03-19

## Full Source / Deobfuscation

- **leeyeel/claude-code-sourcemap** — Full original source extracted from source
  maps accidentally shipped in an early npm release. Anthropic pulled the
  version but it was preserved. Fork at gasxia/claude-code-sourcemap.
- **ghuntley/claude-code-source-code-deobfuscation** (archived) — Cleanroom
  deobfuscation using `webcrack --no-deobfuscate`. The code is minified, not
  obfuscated. Companion transpilation repo uses LLMs. Technique at
  ghuntley.com/tradecraft.
- **memaxo/claude_code_re** — Static analysis of `cli.js`. Multi-backend API
  client (Anthropic, Bedrock, Vertex), layered credential management, tool
  system.

## System Prompts

- **Piebald-AI/claude-code-system-prompts** — Most comprehensive and actively
  maintained. Extracted via script from each npm release. All 18 tools,
  sub-agent prompts, utility prompts. CHANGELOG across 128+ versions.
- **asgeirtj/system_prompts_leaks** — Collection including Claude Code.
- **wong2 gist** (gist.github.com/wong2/e0f34aac66caf890a332f7b6f9e2ba8f) —
  Full tool definitions and system prompt.

## Architectural Analysis

- **Marco Kotrotsos (15-part Medium series)** — Most detailed public analysis.
  v2.0.76. `cli.js` is 10.5MB bundle (app JS + vendored ripgrep + Tree-sitter
  WASM + resvg WASM). Three layers: React/Ink UI, core services, integration.
  Global state object `r0`.
  - Part 1: High-Level Architecture
  - Part 2: The Agent Loop
  - Part 3: Message Structure
  - Part 4: Tool Execution Pipeline
  - Part 6: Session State Management
  - Part 11: Terminal UI
  - Part 12: Request Lifecycle
  - Part 13: Context Management
  - Part 15: Telemetry and Metrics
- **Reid Barber** (reidbarber.com/blog/reverse-engineering-claude-code) — Early
  analysis from source map leak. REPL architecture, services (Statsig, Sentry),
  tools, permission system.
- **Kir Shatrov** (kirshatrov.com/posts/claude-code-internals) — mitmproxy
  approach. Found the "is this a new topic?" classification step.
- **BrightCoding** (blog.brightcoding.dev) — Deep-dive RE report.
- **vrungta** (vrungta.substack.com) — Architecture identifying capability
  primitives: Read, Write, Execute, Connect.
- **Weaxs** (weaxsey.org) — Execution flow and prompt architecture analysis.

## API Interception

- **Yuyz0112/claude-code-reverse** — Intercepts SDK-level API calls. Provides
  `parser.js` and `visualize.html` for analyzing request/response patterns.

## Security Research

- **Adam Chester / XPN** (blog.xpnsec.com/An-Evening-with-Claude-Code/) —
  Found CVE-2025-64755 in v2.0.25. Weak regex allowing arbitrary file writes
  without consent. MCP prompt injection risks.

## Derivative Projects

- **shareAI-lab/learn-claude-code** — "Bash is all you need" nano agent harness.
  Sessions s01-s12 each reverse-engineer one Claude Code mechanism.
- **Ishuin/term-code** — Terminal coder from deobfuscated source + Ollama.

## Key Technical Facts

- `cli.js` is **minified, not obfuscated** — no control flow flattening or
  string encryption. `webcrack --no-deobfuscate` produces readable output.
- The `claude` binary is a **Bun single-file executable** (ELF with embedded
  Bun runtime + bytecode). Trailer marker: `---- Bun! ----`.
- The 10.5MB JS bundle includes: app JS, vendored ripgrep, Tree-sitter WASM
  parsers, resvg WASM.
- Architecture: React/Ink terminal UI -> core agent loop -> Anthropic Messages
  API + MCP.
- Internal telemetry events prefixed with `tengu_`.

## Hacker News Discussions

- Source maps extraction thread (item 43173324)
- ghuntley decompilation thread (item 43217357)
- System prompt 24K tokens thread (item 43909409)
- Sabrina sub-agents RE thread (item 44704138)
