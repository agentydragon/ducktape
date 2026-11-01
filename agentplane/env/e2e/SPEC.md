# Environment test suite

This directory contains end-to-end tests for the Sandbox environment and its coordinated services.
Production contracts live with their owners: [Environment Runtime](../runtime/SPEC.md),
[Harness Host](../harnesshost/SPEC.md), and [Execution Service](../execution/SPEC.md).

Detailed protocol references are split by owner: [Runtime control](../runtime/docs/control.md),
[Harness Host Session protocol](../harnesshost/docs/session_protocol.md), and
[Codex executor integration](../execution/docs/codex_executor.md).

The tests here exercise real Linux/x86_64 harness binaries and container/VM lifecycle behavior;
they run through Bazel on the configured remote execution platform.
