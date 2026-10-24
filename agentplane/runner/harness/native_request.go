package harness

import commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"

type NativeRequestKind string

const (
	ClaudeSubmit       NativeRequestKind = "claude_submit"
	ClaudeModelChange  NativeRequestKind = "claude_model_change"
	ClaudeEffortChange NativeRequestKind = "claude_effort_change"
	CodexTurnStart     NativeRequestKind = "codex_turn_start"
)

// CommandRecord retains admission and terminal event references without copying their facts.
type CommandRecord struct {
	Command         *commandpb.Command
	AdmittedCursor  uint64
	DispatchPlanned bool
	TerminalCursor  *uint64
}

// NativeRequest links one native operation to the commands whose effects it carries.
// A request remains pending while any linked command has no terminal event.
type NativeRequest struct {
	ID       string
	Kind     NativeRequestKind
	Commands []CommandRecord
}
