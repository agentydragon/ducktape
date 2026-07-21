// `hostexec` preview screenshot entry — esbuild bundles this into the `:previews` IIFE. Holds the
// fixtures plus the mount call; `satisfies RegisteredToolPreviewFixture` ties each
// (serverId, toolName, args, result?) to the registry's real Zod schemas, so a stale id, argument,
// or result shape is a type error.
import { mountPreviewCards } from "../screenshot/mount.tsx";

import type { RegisteredToolPreviewFixture } from "../index.tsx";

const PREVIEW_FIXTURES = [
  {
    title: "Search source for TODOs",
    serverId: "hostexec",
    toolName: "bash",
    args: {
      host: "wyrm2",
      run_as: "agentydragon",
      cmd: "rg -n TODO src/",
      max_bytes: 100_000,
      timeout_ms: 30_000,
      cwd: "/home/agentydragon/ducktape",
    },
    result: {
      exit: { kind: "exited", exit_code: 0 },
      stdout: "src/main.py:12:# TODO: handle retries\nsrc/api.py:88:# TODO(agentydragon): dedupe this\n",
      stderr: "",
      duration_ms: 340,
    },
  },
  {
    title: "Restart haproxy as root",
    serverId: "hostexec",
    toolName: "bash",
    args: {
      host: "rugged",
      run_as: "root",
      cmd: "systemctl restart haproxy",
      max_bytes: 100_000,
      timeout_ms: 15_000,
    },
    result: { exit: { kind: "exited", exit_code: 0 }, stdout: "", stderr: "", duration_ms: 890 },
  },
  {
    title: "A failing build command",
    serverId: "hostexec",
    toolName: "bash",
    args: {
      host: "wyrm2",
      run_as: "agentydragon",
      cmd: "cargo build --release 2>&1 | tail -20",
      max_bytes: 100_000,
      timeout_ms: 300_000,
    },
    result: {
      exit: { kind: "exited", exit_code: 101 },
      stdout: "   Compiling finance-augur v0.1.0\n",
      stderr: "error[E0433]: failed to resolve: use of undeclared crate `serde_json`\n",
      duration_ms: 4_210,
    },
  },
] satisfies (RegisteredToolPreviewFixture & { title: string })[];

mountPreviewCards(PREVIEW_FIXTURES);
