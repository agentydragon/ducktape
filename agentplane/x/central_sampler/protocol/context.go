package protocol

import "encoding/json"

type APIStyle string

const (
	APIStyleResponses         APIStyle = "responses"
	APIStyleAnthropicMessages APIStyle = "anthropic_messages"
)

type ExecEncoding string

const (
	ExecEncodingFreeform ExecEncoding = "freeform"
	ExecEncodingJSON     ExecEncoding = "json"
)

type Context interface {
	json.Marshaler
	API() APIStyle
	Records() ([]json.RawMessage, error)
	AppendRecords([]json.RawMessage) error
	AppendUser([]ContentPart) error
	AppendNotice([]ContentPart) error
	AppendToolResult(ToolInvocation, []ContentPart) error
	ChangeEffort(string) error
	AppendExecutionState(*ExecutionState) error
	PendingCalls() ([]ToolInvocation, error)
	FindCall(string) (ToolInvocation, error)
	CheckSettled() error
	TurnComplete() bool
}

// ExecutionState contains only the handles visible to a model.
type ExecutionState struct {
	Cells     []ActiveCell
	Processes []ActiveProcess
}
type (
	ActiveCell struct {
		ID     string
		Status string
	}
	ActiveProcess struct {
		ModelSessionID *int
		Status         string
		TTY            bool
		ExitCode       *int
	}
)

type SessionAttribution struct {
	SessionID string
	ThreadID  string
	TurnID    string
}
