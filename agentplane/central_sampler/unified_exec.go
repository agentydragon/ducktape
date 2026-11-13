package centralsampler

import (
	"bytes"
	"encoding/json"
	"fmt"
	"path/filepath"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/outputbuffer"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/tooloutput"
)

// Codex rust-v0.162.1 core/src/unified_exec/mod.rs yield and output constants.
const (
	minYieldTimeMS            = 250
	minEmptyYieldTimeMS       = 5_000
	maxYieldTimeMS            = 30_000
	maxBackgroundYieldTimeMS  = 300_000
	defaultExecCommandYieldMS = 10_000
	defaultWriteStdinYieldMS  = 250
	interruptInput            = "\x03"
	remoteEnvironmentID       = "remote"
	stdinClosedMessage        = "stdin is closed for this session; rerun exec_command with tty=true to keep stdin open"
	writeToStdinFailedMessage = "failed to write to stdin"
)

// unifiedExecEnvironment is overlaid on every exec_command process.
// Codex rust-v0.162.1 core/src/unified_exec/process_manager.rs UNIFIED_EXEC_ENV.
var unifiedExecEnvironment = map[string]string{
	"NO_COLOR": "1", "TERM": "dumb", "LANG": "C.UTF-8", "LC_CTYPE": "C.UTF-8", "LC_ALL": "C.UTF-8",
	"COLORTERM": "", "PAGER": "cat", "GIT_PAGER": "cat", "GH_PAGER": "cat", "CODEX_CI": "1",
}

// toolError is a caller-visible tool failure. Nested calls reject their JavaScript promise;
// model input errors become failed tool output. toolOutcomeError also rejects nested calls
// when a remote effect may have happened, retaining explicit unknown-outcome evidence.
type toolError struct{ message string }

func (e *toolError) Error() string { return e.message }

type toolOutcomeError struct {
	*toolError
	evidence any
}

func (e *toolOutcomeError) Unwrap() error { return e.toolError }

// Codex rust-v0.162.1 core/src/tools/handlers/unified_exec.rs ExecCommandArgs and
// ExecCommandEnvironmentArgs. Unknown fields are ignored and null means omitted, as with serde.
type execCommandArgs struct {
	Cmd                    *string `json:"cmd"`
	ExpectedIncarnationUID *string `json:"expected_incarnation_uid"`
	Workdir                *string `json:"workdir"`
	Shell                  *string `json:"shell"`
	Login                  *bool   `json:"login"`
	TTY                    *bool   `json:"tty"`
	YieldTimeMS            *uint64 `json:"yield_time_ms"`
	MaxOutputTokens        *uint64 `json:"max_output_tokens"`
}

// Codex rust-v0.162.1 core/src/tools/handlers/unified_exec/write_stdin.rs WriteStdinArgs.
type writeStdinArgs struct {
	SessionID       *int32  `json:"session_id"`
	Chars           *string `json:"chars"`
	YieldTimeMS     *uint64 `json:"yield_time_ms"`
	MaxOutputTokens *uint64 `json:"max_output_tokens"`
}

// decodeFunctionArguments mirrors upstream nested-call argument handling: an absent input is `{}`,
// a non-object input is rejected, and parse failures are reported to the model.
// Codex rust-v0.162.1 core/src/tools/code_mode/mod.rs serialize_function_tool_arguments and
// core/src/tools/handlers/mod.rs parse_arguments.
func decodeFunctionArguments(tool string, input json.RawMessage, value any) error {
	trimmed := bytes.TrimSpace(input)
	if len(trimmed) == 0 || bytes.Equal(trimmed, []byte("null")) {
		trimmed = []byte("{}")
	}
	if trimmed[0] != '{' {
		return &toolError{fmt.Sprintf("tool `%s` expects a JSON object for arguments", tool)}
	}
	if err := json.Unmarshal(trimmed, value); err != nil {
		return &toolError{"failed to parse function arguments: " + err.Error()}
	}
	return nil
}

func missingField(name string) error {
	return &toolError{fmt.Sprintf("failed to parse function arguments: missing field `%s`", name)}
}

// execShell is the shell an executor reports at initialization.
// Codex rust-v0.162.1 exec-server-protocol/src/protocol.rs ShellInfo.
type execShell struct {
	Name string `json:"name"`
	Path string `json:"path"`
}

// fallbackExecShell stands in when an exec-server reports no shell. Upstream would use the
// Codex host user's shell, which the sampler does not have.
var fallbackExecShell = execShell{Name: "sh", Path: "/bin/sh"}

// detectShellType names a shell by its executable, or "" when unrecognized.
// Codex rust-v0.162.1 shell-command/src/shell_detect.rs detect_shell_type.
func detectShellType(path string) string {
	switch path {
	case "zsh", "sh", "cmd", "bash":
		return path
	case "pwsh", "powershell":
		return "powershell"
	}
	stem := strings.TrimSuffix(filepath.Base(path), filepath.Ext(path))
	if stem != "" && stem != path {
		return detectShellType(stem)
	}
	return ""
}

// execCommandPlan is a validated exec_command ready to dispatch.
type execCommandPlan struct {
	argv            []string
	cwd             string
	tty             bool
	yieldMS         int
	maxOutputTokens *int
}

// parseExecCommand validates arguments without requiring the remote environment's shell.
func parseExecCommand(input json.RawMessage) (execCommandArgs, error) {
	var args execCommandArgs
	if err := decodeFunctionArguments("exec_command", input, &args); err != nil {
		return args, err
	}
	if args.Cmd == nil {
		return args, missingField("cmd")
	}
	if args.ExpectedIncarnationUID != nil {
		uid := *args.ExpectedIncarnationUID
		if uid == "" || strings.TrimSpace(uid) != uid || len(uid) > 256 {
			return args, &toolError{"failed to parse function arguments: `expected_incarnation_uid` must be a non-empty, unpadded UID of at most 256 bytes"}
		}
	}
	return args, nil
}

// planExecCommand validates arguments and resolves the command line.
// Codex rust-v0.162.1 core/src/tools/handlers/unified_exec/exec_command.rs
// ExecCommandHandler::handle_call and unified_exec.rs get_command, for a remote environment.
func planExecCommand(input json.RawMessage, cwd string, shell *execShell) (*execCommandPlan, error) {
	args, err := parseExecCommand(input)
	if err != nil {
		return nil, err
	}
	return args.plan(cwd, shell)
}

func (args execCommandArgs) plan(cwd string, shell *execShell) (*execCommandPlan, error) {
	if args.Workdir != nil && *args.Workdir != "" {
		if filepath.IsAbs(*args.Workdir) {
			cwd = filepath.Clean(*args.Workdir)
		} else {
			cwd = filepath.Join(cwd, *args.Workdir)
		}
	}
	// A remote environment validates a requested shell against the one it reports, then always
	// launches the reported shell.
	if args.Shell != nil {
		if shell == nil {
			return nil, &toolError{fmt.Sprintf("environment `%s` does not report a shell", remoteEnvironmentID)}
		}
		if detectShellType(*args.Shell) != detectShellType(shell.Name) {
			return nil, &toolError{fmt.Sprintf("environment `%s` only supports `%s`", remoteEnvironmentID, shell.Name)}
		}
	}
	launch := fallbackExecShell
	if shell != nil {
		launch = *shell
	}
	login := args.Login == nil || *args.Login
	var argv []string
	switch detectShellType(launch.Name) {
	case "zsh", "bash", "sh":
		flag := "-c"
		if login {
			flag = "-lc"
		}
		argv = []string{launch.Path, flag, *args.Cmd}
	default:
		return nil, &toolError{fmt.Sprintf("environment `%s` reports unsupported shell `%s`", remoteEnvironmentID, launch.Name)}
	}
	yieldMS := uint64(defaultExecCommandYieldMS)
	if args.YieldTimeMS != nil {
		yieldMS = *args.YieldTimeMS
	}
	return &execCommandPlan{
		argv: argv, cwd: cwd, tty: args.TTY != nil && *args.TTY,
		yieldMS: int(min(max(yieldMS, minYieldTimeMS), maxYieldTimeMS)), maxOutputTokens: tokenBudget(args.MaxOutputTokens),
	}, nil
}

func tokenBudget(value *uint64) *int {
	if value == nil {
		return nil
	}
	// Collected output never exceeds 1 MiB, so any larger budget behaves the same; the bound only
	// keeps byte arithmetic in range.
	budget := int(min(*value, outputbuffer.MaxBytes))
	return &budget
}

// writeStdinPlan is a validated write_stdin request.
type writeStdinPlan struct {
	sessionID       int
	chars           string
	yieldMS         int
	maxOutputTokens *int
}

// Codex rust-v0.162.1 core/src/unified_exec/process_manager.rs write_stdin_inner yield bounds.
func planWriteStdin(input json.RawMessage) (*writeStdinPlan, error) {
	var args writeStdinArgs
	if err := decodeFunctionArguments("write_stdin", input, &args); err != nil {
		return nil, err
	}
	if args.SessionID == nil {
		return nil, missingField("session_id")
	}
	plan := &writeStdinPlan{sessionID: int(*args.SessionID), maxOutputTokens: tokenBudget(args.MaxOutputTokens)}
	if args.Chars != nil {
		plan.chars = *args.Chars
	}
	yieldMS := uint64(defaultWriteStdinYieldMS)
	if args.YieldTimeMS != nil {
		yieldMS = *args.YieldTimeMS
	}
	yieldMS = max(yieldMS, minYieldTimeMS)
	if plan.chars == "" {
		yieldMS = min(max(yieldMS, minEmptyYieldTimeMS), maxBackgroundYieldTimeMS)
	} else {
		yieldMS = min(yieldMS, maxYieldTimeMS)
	}
	plan.yieldMS = int(yieldMS)
	return plan, nil
}

// unifiedExecResult is what a nested exec_command or write_stdin call resolves to in JavaScript.
// Codex rust-v0.162.1 core/src/tools/context.rs ExecCommandToolOutput::code_mode_result.
type unifiedExecResult struct {
	ChunkID            string  `json:"chunk_id,omitempty"`
	WallTimeSeconds    float64 `json:"wall_time_seconds"`
	ExitCode           *int    `json:"exit_code,omitempty"`
	SessionID          *int    `json:"session_id,omitempty"`
	OriginalTokenCount *int    `json:"original_token_count,omitempty"`
	Output             string  `json:"output"`
}

// processObservation is output collected from one process interaction.
type processObservation struct {
	output   *outputbuffer.Buffer
	exitCode *int
	exited   bool
	wallTime float64
}

func (o processObservation) unifiedExecResult(sessionID *int, maxOutputTokens *int, chunkID string) *unifiedExecResult {
	collected := o.output.WithOmissionMarker()
	// Codex rust-v0.162.1 utils/string/src/truncate.rs approx_tokens_from_byte_count.
	tokens := tooloutput.ApproxTokenCountBytes(o.output.TotalBytes())
	result := &unifiedExecResult{ChunkID: chunkID, WallTimeSeconds: o.wallTime, ExitCode: o.exitCode, OriginalTokenCount: &tokens}
	if !o.exited {
		result.SessionID = sessionID
	}
	if maxOutputTokens != nil {
		result.Output = tooloutput.TruncateExecOutput(collected, o.output.OmittedBytes(), tokens, *maxOutputTokens)
	} else {
		result.Output = tooloutput.LossyUTF8(collected)
	}
	return result
}
