package toolcontract

import "github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"

const outputTokenBudgetPropertyDoc = "Output token budget. Defaults to 10000 tokens; larger requests may be capped by policy."

func schemaString(description string) protocol.JSONSchema {
	return protocol.JSONSchema{Type: "string", Description: description}
}

func schemaNumber(description string) protocol.JSONSchema {
	return protocol.JSONSchema{Type: "number", Description: description}
}

func schemaBoolean(description string) protocol.JSONSchema {
	return protocol.JSONSchema{Type: "boolean", Description: description}
}

func execOutputSchema() *protocol.JSONSchema {
	closed := false
	return &protocol.JSONSchema{
		Type: "object", Required: []string{"wall_time_seconds", "output"}, AdditionalProperties: &closed,
		Properties: map[string]protocol.JSONSchema{
			"chunk_id":             schemaString("Chunk identifier included when the response reports one."),
			"wall_time_seconds":    schemaNumber("Elapsed wall time spent waiting for output in seconds."),
			"exit_code":            schemaNumber("Process exit code when known."),
			"session_id":           schemaNumber("Session identifier to pass to write_stdin while the process is running or its output remains open."),
			"original_token_count": schemaNumber("Approximate token count before truncation."),
			"output":               schemaString("Command output text, possibly truncated."),
		},
	}
}

// ExecCommandDefinition is upstream's Linux exec_command for Agentplane's wrapped-Codex
// configuration: one remote environment (no environment_id), login shells allowed (default
// allow_login_shell, so `login` is offered) and unified_exec_tty on.
// Deviation: upstream always offers the approval parameters `sandbox_permissions`, `justification`
// and `prefix_rule`; they are omitted because Agentplane's Sandbox, egress and Action Service own
// those boundaries (SPEC.md).
// Codex rust-v0.162.1 core/src/tools/handlers/shell_spec.rs create_exec_command_tool_with_environment_id.
func ExecCommandDefinition() protocol.CodeToolDefinition {
	closed := false
	return protocol.CodeToolDefinition{
		Name: "exec_command", ToolName: protocol.ToolName{Name: "exec_command"},
		Description: "Runs a command in a PTY, returning output or a session ID for ongoing interaction.", Kind: "function",
		InputSchema: &protocol.JSONSchema{
			Type: "object", Required: []string{"cmd"}, AdditionalProperties: &closed,
			Properties: map[string]protocol.JSONSchema{
				"cmd":                      schemaString("Shell command to execute."),
				"expected_incarnation_uid": schemaString("Optional execution Sandbox incarnation UID from authorized environment inspection. Rejects this command if the target has changed incarnation."),
				"workdir":                  schemaString("Working directory for the command. Defaults to the turn cwd."),
				"tty":                      schemaBoolean("True allocates a PTY for the command; false or omitted uses plain pipes."),
				"yield_time_ms":            schemaNumber("Wait before yielding output. Defaults to 10000 ms; effective range is 250-30000 ms."),
				"max_output_tokens":        schemaNumber(outputTokenBudgetPropertyDoc),
				"shell":                    schemaString("Shell binary to launch. Defaults to the user's default shell."),
				"login":                    schemaBoolean("True runs the shell with -l/-i semantics; false disables them. Defaults to true."),
			},
		},
		OutputSchema: execOutputSchema(),
	}
}

// Codex rust-v0.162.1 core/src/tools/handlers/shell_spec.rs create_write_stdin_tool.
func WriteStdinDefinition() protocol.CodeToolDefinition {
	closed := false
	return protocol.CodeToolDefinition{
		Name: "write_stdin", ToolName: protocol.ToolName{Name: "write_stdin"},
		Description: "Writes characters to an existing unified exec session and returns recent output.", Kind: "function",
		InputSchema: &protocol.JSONSchema{
			Type: "object", Required: []string{"session_id"}, AdditionalProperties: &closed,
			Properties: map[string]protocol.JSONSchema{
				"session_id":        schemaNumber("Identifier of the unified exec session, including one with output still open after exit."),
				"chars":             schemaString("Bytes to write to stdin. Defaults to empty, which polls without writing."),
				"yield_time_ms":     schemaNumber("Wait before yielding output. Non-empty writes default to 250 ms and cap at 30000 ms; empty polls wait 5000-300000 ms by default."),
				"max_output_tokens": schemaNumber(outputTokenBudgetPropertyDoc),
			},
		},
		OutputSchema: execOutputSchema(),
	}
}

// Codex rust-v0.162.1 core/src/tools/handlers/view_image_spec.rs create_view_image_tool with
// ViewImageToolOptions all false: the sampler has no model catalog to enable original detail.
func ViewImageDefinition() protocol.CodeToolDefinition {
	closed := false
	detail := schemaString("Image detail hint returned by view_image. Returns `high` for default resized behavior or `original` when original resolution is preserved.")
	detail.Enum = []string{"high", "original"}
	return protocol.CodeToolDefinition{
		Name: "view_image", ToolName: protocol.ToolName{Name: "view_image"},
		Description: "View a local image file from the filesystem when visual inspection is needed. Use this for images already available on disk.",
		Kind:        "function",
		InputSchema: &protocol.JSONSchema{
			Type: "object", Required: []string{"path"}, AdditionalProperties: &closed,
			Properties: map[string]protocol.JSONSchema{"path": schemaString("Local filesystem path to an image file.")},
		},
		OutputSchema: &protocol.JSONSchema{
			Type: "object", Required: []string{"image_url", "detail"}, AdditionalProperties: &closed,
			Properties: map[string]protocol.JSONSchema{"image_url": schemaString("Data URL for the loaded image."), "detail": detail},
		},
	}
}

// Definitions builds the sampler's native provider tool definitions.
func Definitions() []protocol.CodeToolDefinition {
	return []protocol.CodeToolDefinition{
		ExecCommandDefinition(), WriteStdinDefinition(),
		{Name: "apply_patch", ToolName: protocol.ToolName{Name: "apply_patch"}, Description: "Apply a Codex *** Begin Patch / *** End Patch patch through upstream apply-patch.", Kind: "freeform"},
		ViewImageDefinition(),
		// TODO: update_plan and Action-backed (MCP) tools, which Codex also nests.
	}
}

// ModelSurface assembles and validates the provider-facing model surface from the native tools.
func ModelSurface(encoding protocol.ExecEncoding) (protocol.ToolSurface, error) {
	description, err := ExecToolDescription(Definitions(), encoding)
	if err != nil {
		return protocol.ToolSurface{}, err
	}
	parameters, err := JSONExecToolParameters()
	if err != nil {
		return protocol.ToolSurface{}, err
	}
	return protocol.ToolSurface{
		Description: description, Parameters: parameters, FreeformGrammar: ExecFreeformGrammar,
		WaitDescription: WaitToolDescription(), WaitParameters: WaitToolParameters(),
	}, nil
}
