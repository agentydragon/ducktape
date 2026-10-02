import type { SessionEvent, SessionSummary } from "../api";

// Synthetic, shape-matched to a hook-heavy session: about half hooks, one fifth
// assistant records, one eighth user/tool results, and the rest control traffic.
// All prose, identifiers, paths, timestamps and output are invented.
export const noisySession: SessionSummary = {
  id: "session_fixture_precision",
  title: "Check sample meter precision",
  status: "archived",
  created_at: "2026-01-01T12:00:00Z",
  updated_at: "2026-01-01T12:02:00Z",
  last_event_at: "2026-01-01T12:02:00Z",
};

export const longCommandActivityTitle =
  "Run the focused browser checks with all report artifacts\nnpm test -- --runInBand --reporter=default --coverage --outputFile=artifacts/session-summary/browser-results.json --config=visual/session-summary.config.mjs\nthen collect the local HTML report and attach it to the review summary";
export const longCommandActivityDetail =
  "Synthetic command activity completed successfully.\nThe browser checks passed and the HTML report is ready.";

function makeEvents(includeLongCommandActivity = false): SessionEvent[] {
  const events: SessionEvent[] = [];
  function add(
    event_type: string,
    payload: Record<string, unknown>,
    source = event_type === "control_request" ? "client" : "worker"
  ): void {
    const sequence = events.length + 1;
    events.push({
      event_id: `00000000-0000-4000-8000-${String(sequence).padStart(12, "0")}`,
      sequence_num: String(sequence),
      event_type,
      source,
      created_at: new Date(Date.parse(noisySession.created_at) + sequence * 1000).toISOString(),
      received_at: null,
      processing_at: null,
      processed_at: null,
      device_attestation_status: "DEVICE_ATTESTATION_STATUS_UNSPECIFIED",
      sent_by_account_id: null,
      payload: { type: event_type, ...payload },
    });
  }
  function hook(hook_event: string, id: string): void {
    const identity = { hook_event, hook_id: id, hook_name: `${hook_event}:fixture-check`, session_id: noisySession.id };
    add("system", { ...identity, subtype: "hook_started" });
    add("system", {
      ...identity,
      subtype: "hook_response",
      exit_code: 0,
      outcome: "success",
      stdout: "",
      stderr: Array.from({ length: 12 }, (_, i) => `fixture-check: verified sample rule ${i + 1}`).join("\n"),
      output: "Fixture checks completed successfully.",
    });
  }
  function message(role: "user" | "assistant", content: Record<string, unknown>[], extra = {}): void {
    add(
      role,
      { message: { role, content }, ...extra },
      role === "user" && content.some((block) => block.type === "text") ? "client" : "worker"
    );
  }

  add("system", { subtype: "init", session_id: noisySession.id, cwd: "/workspace/sample-meter" });
  add("env_manager_log", { data: { level: "info", message: "Starting fixture environment" } });
  add("control_request", { request_id: "fixture-init", request: { subtype: "initialize" } });
  add("control_response", { response: { request_id: "fixture-init", subtype: "success" } });
  for (let i = 0; i < 6; i += 1) hook("SessionStart", `fixture-start-${i}`);
  add("mcp_auth_required", { server_name: "fixture-docs", authorization_url: "https://auth.example.test/login" });
  add("active_goal", { goal: "Investigate sample meter rounding" });
  add("autocompact_state", { state: "idle" });
  message("user", [
    { type: "text", text: "The sample meter rounds too early. Find the source and preserve precision until display." },
  ]);
  message("assistant", [
    { type: "text", text: "I’ll trace the response through the parser and the display formatter." },
  ]);
  add("control_request", { request_id: "fixture-mode", request: { subtype: "set_permission_mode", mode: "default" } });
  add("control_response", { response: { request_id: "fixture-mode", subtype: "success" } });
  add("control_request", { request_id: "fixture-mcp", request: { subtype: "mcp_set_servers", servers: {} } });
  add("system", { subtype: "status", status: null });

  for (let i = 0; i < 12; i += 1) {
    const id = `fixture-tool-${i}`;
    const name = i === 9 ? "Task" : i % 3 === 0 ? "Read" : i % 3 === 1 ? "Bash" : "Grep";
    const input =
      name === "Read"
        ? { file_path: "/workspace/sample-meter/src/precision.ts" }
        : name === "Bash"
          ? { command: "cat fixtures/sample-response.json", description: "Inspect the sample response" }
          : name === "Task"
            ? { description: "Check the fixture parser", prompt: "Inspect rounding in the sample parser." }
            : { pattern: "round|precision", path: "/workspace/sample-meter/src" };
    // A child tool/result stays correlated with its Task without becoming a message.
    const extra = i === 10 ? { parent_tool_use_id: "fixture-tool-9" } : {};
    message("assistant", [
      {
        type: "thinking",
        thinking: i % 4 === 0 ? "Check where the fractional digits are lost." : "",
        signature: "fixture-signature",
      },
    ]);
    message("assistant", [{ type: "tool_use", id, name, input }], extra);
    hook("PreToolUse", `fixture-pre-${i}`);
    const output =
      name === "Read"
        ? "     1→export const sample = 12.3456;\n     2→export const display = sample.toFixed(2);"
        : name === "Task"
          ? "The parser retains precision; the display rounds to two places."
          : Array.from({ length: 8 }, (_, line) => `fixture record ${line + 1}: value=12.3456`).join("\n");
    message("user", [{ type: "tool_result", tool_use_id: id, content: output }], {
      ...extra,
      ...(name === "Read"
        ? {
            tool_use_result: {
              type: "text",
              file: {
                filePath: input.file_path,
                content: "export const sample = 12.3456;\nexport const display = sample.toFixed(2);",
                startLine: 1,
                numLines: 2,
                totalLines: 2,
              },
            },
          }
        : {}),
    });
    hook("PostToolUse", `fixture-post-${i}`);
    if (i === 5) {
      message("user", [{ type: "text", text: "Keep the full value in the API response; round only the label." }]);
      add("system", { subtype: "background_tasks_changed", tasks: [] });
      add("env_manager_log", { data: { level: "info", message: "Fixture worker heartbeat" } });
    }
    if (i === 9) {
      add("system", {
        subtype: "task_started",
        task_id: "fixture-task",
        tool_use_id: id,
        description: "Check the fixture parser",
      });
      add("tool_progress", { tool_use_id: id, tool_name: "Task", elapsed_time_seconds: 2 });
      add("system", {
        subtype: "task_notification",
        task_id: "fixture-task",
        status: "completed",
        summary: "Parser preserves fractional digits.",
      });
    }
  }
  if (includeLongCommandActivity) {
    add("system", {
      subtype: "task_started",
      task_id: "fixture-long-command",
      description: longCommandActivityTitle,
    });
    add("system", {
      subtype: "task_notification",
      task_id: "fixture-long-command",
      status: "completed",
      summary: longCommandActivityDetail,
    });
  }
  message("assistant", [
    {
      type: "text",
      text: "The response keeps 12.3456 intact. Only the display label rounds it to 12.35.\n\nThe fixture now checks that serialization preserves all fractional digits.",
    },
  ]);
  add("result", { subtype: "success", is_error: false, duration_ms: 1200, total_cost_usd: 0.01, num_turns: 2 });
  add("rate_limit_event", { rate_limit_info: { status: "allowed" } });
  add("prompt_suggestion", { suggestion: "Check the alternate format" });
  add("env_manager_log", { data: { level: "info", message: "Fixture worker stopped" } });
  return events;
}

export const noisySessionEvents: SessionEvent[] = makeEvents();
export const longCommandActivitySessionEvents: SessionEvent[] = makeEvents(true);
