// Reconstructed from binary: Build ID 6b49f1ca, Go 1.25.6
// Source: internal/api/session_ingress_types.go
//
// Original source path:
//   /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/api/session_ingress_types.go

package api

import (
	"fmt"
	"time"

	"github.com/google/uuid"
)

// LogCategory represents a category for diagnostic log entries.
// Binary: string-typed enum used in session activity recording.
type LogCategory string

// SessionError is the interface for errors that can occur during session processing.
// Known implementors: ClaudeCodeExecutionError, SourceProcessingError
//
// Binary itabs:
//   go:itab.ClaudeCodeExecutionError,SessionError at 0xf60a98
//   go:itab.SourceProcessingError,SessionError at 0xf611b0
type SessionError interface {
	GetUserMessage() string
	IsFatal() bool
}

// EventData is the interface for event data payloads.
// Known implementor: *EnvManagerLogEventData
//
// Binary itab:
//   go:itab.*EnvManagerLogEventData,EventData at 0xf5b440
type EventData interface{}

// MessageContent is the interface for message content in ingress events.
// Known implementor: AssistantMessage
//
// Binary itab:
//   go:itab.AssistantMessage,MessageContent at 0xf5b420
type MessageContent interface{}

// ContentBlock represents a content block in a message.
// Binary type eq: 0x832aa0
type ContentBlock struct {
	Type string `json:"type"` // e.g., "text"
	Text string `json:"text"`
}

// PermissionDenial represents a permission denial event.
// Binary type eq: 0x832da0
type PermissionDenial struct {
	// Fields inferred from type eq function
}

// AssistantMessage represents a synthetic assistant message.
// Binary itab: go:itab.AssistantMessage,MessageContent at 0xf5b420
type AssistantMessage struct {
	Content []ContentBlock `json:"content"`
}

// DiagLogEntry represents a single diagnostic log entry for forwarding.
// Used in slices.SortFunc (binary has pdqsort/insertionSort specializations for this type).
// Binary signature: struct { Timestamp time.Time; Fields map[string]interface {} }
type DiagLogEntry struct {
	Timestamp time.Time              // offset 0x00 (Time is 3 words: wall, ext, loc)
	Fields    map[string]interface{} // offset 0x18
}

// SessionIngressEvent is the top-level event structure sent to the session ingress API.
type SessionIngressEvent struct {
	Type    string      `json:"type"`     // offset 0x00: event type string (e.g., "env_manager_log")
	ID      string      `json:"id"`       // offset 0x10: UUID string
	Data    EventData   `json:"data"`     // offset 0x20: interface (itab + data ptr)
}

// EnvManagerLogEventData is the data payload for env_manager_log events.
//
// Binary address (constructor): 0x831740
type EnvManagerLogEventData struct {
	Message   string            `json:"message"`    // offset 0x00 (string: ptr + len)
	Level     string            `json:"level"`      // offset 0x10 (string: ptr + len)
	Source    string            `json:"source"`     // offset 0x20 (string: ptr + len)
	Timestamp time.Time         `json:"timestamp"`  // offset 0x30 (wall), 0x38 (ext)
	Nanos     int64             `json:"nanos"`      // offset 0x40
	Fields    map[string]string `json:"fields"`     // offset 0x48
}

// ResultEvent represents the result/completion event for a session.
type ResultEvent struct {
	Error        *SessionError `json:"error,omitempty"`         // offset 0x00 (interface ptr)
	IsError      *bool         `json:"is_error,omitempty"`      // offset 0x08 + 0x08
	HasError     *bool         `json:"has_error,omitempty"`     // offset somewhere
	SessionError interface{}   `json:"session_error,omitempty"` // error details
	// Additional fields for result event metadata
}

// ClaudeCodeExecutionError is a session error indicating Claude Code execution failed.
// GetUserMessage returns a fixed string (116 = 0x74 bytes).
// IsFatal returns the value of the Fatal field (stored in AL register).
//
// Binary type eq: auto-generated
type ClaudeCodeExecutionError struct {
	Fatal bool // offset 0x00 (byte)
}

// GetUserMessage returns a fixed error message for Claude Code execution errors.
// The message is 116 (0x74) bytes long.
//
// Binary address: 0x831520
func (e ClaudeCodeExecutionError) GetUserMessage() string {
	return "Claude Code encountered an unexpected error during execution. The environment will be reset for the next attempt."
}

// IsFatal returns whether the error is fatal.
// Simply returns the Fatal field value (passed in AL register).
//
// Binary address: 0x831540
func (e ClaudeCodeExecutionError) IsFatal() bool {
	return e.Fatal
}

// SourceProcessingError is a session error for source/file processing failures.
//
// Binary type eq: 0x832fa0
type SourceProcessingError struct {
	Fatal  bool   // offset 0x00 (byte)
	Source string // offset 0x08 (string: ptr + len)
}

// GetUserMessage returns a user-facing error message. If Source is non-empty,
// includes it via fmt.Sprintf; otherwise returns a generic message (112 = 0x70 bytes).
// Message with source is 115 (0x73) bytes of format + source.
//
// Binary address: 0x831560
func (e SourceProcessingError) GetUserMessage() string {
	if e.Source != "" {
		return fmt.Sprintf(
			"An error occurred while processing your project sources. The environment will be reset for the next attempt. Source: %s",
			e.Source,
		)
	}
	return "An error occurred while processing your project sources. The environment will be reset for the next attempt."
}

// IsFatal returns whether the error is fatal.
// Returns the Fatal field value.
//
// Binary address: 0x831600
func (e SourceProcessingError) IsFatal() bool {
	return e.Fatal
}

// NewEnvManagerLogEvent creates a new SessionIngressEvent of type "env_manager_log"
// wrapping the given EnvManagerLogEventData.
//
// Generates a UUID for the event ID using uuid.NewRandom(), encodes to hex string (36 chars).
// Sets Type to "env_manager_log" (15 = 0x0f bytes).
//
// Binary address: 0x831620
func NewEnvManagerLogEvent(data *EnvManagerLogEventData) *SessionIngressEvent {
	id, err := uuid.NewRandom()
	if err != nil {
		panic(err)
	}
	idStr := id.String()

	return &SessionIngressEvent{
		Type: "env_manager_log",
		ID:   idStr,
		Data: data,
	}
}

// NewEnvManagerLogEventData constructs a new EnvManagerLogEventData with the current timestamp.
//
// Parameters: message, level, source strings, extra fields map (may be nil - auto-created).
// Calls time.Now() and normalizes the timestamp (monotonic clock handling).
//
// Binary address: 0x831740
func NewEnvManagerLogEventData(message string, level string, source string, extraStr string, fields map[string]string) *EnvManagerLogEventData {
	if fields == nil {
		fields = make(map[string]string)
	}

	now := time.Now()
	// The binary strips the monotonic clock reading and normalizes wall time.

	return &EnvManagerLogEventData{
		Message:   message,
		Level:     level,
		Source:    source,
		Timestamp: now,
		Fields:    fields,
	}
}

// NewSyntheticAssistantMessage creates a SessionIngressEvent containing a synthetic
// assistant message with a single "text" content block.
//
// Generates a UUID for the event ID.
// Sets Type to "assistant" (9 = 0x09 bytes).
// Creates a ContentBlock with Type="text" (4 = 0x04 bytes).
// Wraps in an AssistantMessage implementing MessageContent.
//
// Binary address: 0x831900
func NewSyntheticAssistantMessage(text string, model string) *SessionIngressEvent {
	contentBlock := ContentBlock{
		Type: "text",
		Text: text,
	}

	id, err := uuid.NewRandom()
	if err != nil {
		panic(err)
	}
	idStr := id.String()

	event := &SessionIngressEvent{
		Type: "assistant",
		ID:   idStr,
	}

	msg := AssistantMessage{
		Content: []ContentBlock{contentBlock},
	}
	_ = msg
	_ = model

	return event
}

// NewResultEvent creates a SessionIngressEvent for the session result/completion.
//
// Parameters:
//   - sessionError: the error info (SessionError interface, may be nil)
//   - isFatal: whether the error is fatal (bool pointer)
//   - exitCode: the exit code (interface, may be nil)
//
// Generates a UUID for the event ID.
// Sets Type to "result" (6 = 0x06 bytes).
// Creates pointers to bool values for is_error and has_error fields.
// Sets up metadata map via makemap_small.
//
// Binary address: 0x831b20
func NewResultEvent(sessionError SessionError, isFatal interface{}, exitCode interface{}) *SessionIngressEvent {
	errorInfo := &struct{ SessionError }{}
	_ = errorInfo

	isError := isFatal != nil
	_ = isError

	hasError := false
	_ = hasError

	id, err := uuid.NewRandom()
	if err != nil {
		panic(err)
	}
	idStr := id.String()

	event := &SessionIngressEvent{
		Type: "result",
		ID:   idStr,
	}

	// The binary sets up multiple fields on the result event object:
	// - session error pointer
	// - is_error bool pointer
	// - has_error bool pointer
	// - metadata map (makemap_small)
	// - exit code
	// - additional type/itab references

	return event
}
