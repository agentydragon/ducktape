// Reconstructed from binary: Build ID 495ea204
// Source: internal/api/session_ingress_types.go
//
// Original source path:
//   /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/api/session_ingress_types.go
//
// ---------------------------------------------------------------------------
// RE-VERIFICATION against Build ID 0b86a2a0 (release-1186d93b9-ext), from Go
// runtime type metadata rather than disassembly guessing.
//
// VERDICT: the session-ingress event payload types are **byte-for-byte
// unchanged** between release-d84d76b7-ext and release-1186d93b9-ext — same
// fields, same json tags, same offsets. Nothing in this file's wire contract
// changed in this release.
//
// BUT several of the shapes below do not match the binary. Corrections, with
// the RTTI addresses that prove them (garbled package `fHxyBOR9qvy` = the
// session-ingress side of internal/api; previous binary's `viRrDTePbcGS`):
//
//	SessionIngressEvent  -> fHxyBOR9qvy.AIGJ5cph, vaddr 0x28eaa00, size 0xc0,
//	                        16 fields (previous: viRrDTePbcGS.J4sedR @0x247a760,
//	                        identical). The real shape is NOT {type,id,data}:
//	  type                string  `json:"type"`                          // +0x00
//	  uuid                string  `json:"uuid"`                          // +0x10  <- NOT "id"
//	  data                <iface> `json:"data,omitempty"`                // +0x20
//	  message             <iface> `json:"message,omitempty"`             // +0x30
//	  parent_tool_use_id  *string `json:"parent_tool_use_id,omitempty"`  // +0x40
//	  isApiErrorMessage   *bool   `json:"isApiErrorMessage,omitempty"`   // +0x48
//	  subtype             *string `json:"subtype,omitempty"`             // +0x50
//	  is_error            *bool   `json:"is_error,omitempty"`            // +0x58
//	  duration_ms         *int    `json:"duration_ms,omitempty"`         // +0x60
//	  duration_api_ms     *int    `json:"duration_api_ms,omitempty"`     // +0x68
//	  num_turns           *int    `json:"num_turns,omitempty"`           // +0x70
//	  total_cost_usd  *float64    `json:"total_cost_usd,omitempty"`      // +0x78
//	  errors            []string  `json:"errors,omitempty"`              // +0x80
//	  modelUsage         <named>  `json:"modelUsage,omitempty"`          // +0x98
//	  permission_denials []Denial `json:"permission_denials,omitempty"`  // +0xa0
//	  usage              *Usage   `json:"usage,omitempty"`               // +0xb8
//
//	AssistantMessage     -> fHxyBOR9qvy.Labalu1jJu, vaddr 0x28a9c00, size 0x68:
//	                        {role, model, content []ContentBlock, stop_reason,
//	                        usage} — the RE below models only `content`.
//	ContentBlock         -> fHxyBOR9qvy.IBq8n0_LLk, vaddr 0x27c1ec0:
//	                        {type, text `json:"text,omitempty"`} — correct.
//	PermissionDenial     -> fHxyBOR9qvy.Ai2IVr3VUa, vaddr 0x27e77e0:
//	                        {tool_name, reason, requested_at time.Time} — the
//	                        RE below leaves it empty.
//	Usage                -> fHxyBOR9qvy.TRepBeaLEAWm, vaddr 0x2883260:
//	                        {input_tokens, output_tokens,
//	                         cache_creation_input_tokens,omitempty,
//	                         cache_read_input_tokens,omitempty}
//	Session activity log -> fHxyBOR9qvy.C55iIMbPNU, vaddr 0x28a9d00, size 0x50:
//	                        {level, category, content, timestamp time.Time,
//	                         extra map[string]any}
//	DiagLogEntry         -> fHxyBOR9qvy.Ur1ssf, vaddr 0x27c1f60, size 0x20:
//	                        {time.Time, map[string]any} — matches the RE below.
//
//	EnvManagerLogEventData: NO struct type in either binary carries the tag set
//	{message, level, source, timestamp, nanos, fields}. The `nanos` tag in this
//	build belongs to google.protobuf.Timestamp only, and "env_manager_log"
//	appears in neither binary nor either decrypted core. The type was invented;
//	it has been removed (see the REMOVED note further down).
//
// The declarations below have been rewritten against the addresses above.
// Types marked VERIFIED are field-for-field from the binary's type metadata:
// the field names are garble-randomized, but the json tags, Go types and byte
// offsets are exact.
// ---------------------------------------------------------------------------

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
//
//	go:itab.ClaudeCodeExecutionError,SessionError at 0xf60a98
//	go:itab.SourceProcessingError,SessionError at 0xf611b0
type SessionError interface {
	GetUserMessage() string
	IsFatal() bool
}

// EventData is the interface carried in SessionIngressEvent.Data.
//
// Binary: fHxyBOR9qvy.GeaI7aFBat7, the declared type of the field at +0x20.
//
// TODO(re): no implementor identified in the current binary. The itab
// go:itab.*EnvManagerLogEventData,EventData cited here previously came from the
// a6f96673 build and does not exist in 0b86a2a0 -- see the REMOVED note near
// the bottom of this file.
type EventData interface{}

// MessageContent is the interface carried in SessionIngressEvent.Message.
//
// Binary: fHxyBOR9qvy.CCaEAoszQG, the declared type of the field at +0x30.
// AssistantMessage is an implementor.
type MessageContent interface{}

// ContentBlock represents a content block in a message.
//
// Binary: fHxyBOR9qvy.IBq8n0_LLk, vaddr 0x27c1ec0, size 0x20. VERIFIED.
type ContentBlock struct {
	Type string `json:"type"`           // +0x00, e.g. "text"
	Text string `json:"text,omitempty"` // +0x10
}

// PermissionDenial records a tool invocation the user refused.
//
// Binary: fHxyBOR9qvy.Ai2IVr3VUa, vaddr 0x27e77e0, size 0x38. VERIFIED.
type PermissionDenial struct {
	ToolName    string    `json:"tool_name"`    // +0x00
	Reason      string    `json:"reason"`       // +0x10
	RequestedAt time.Time `json:"requested_at"` // +0x20
}

// Usage is the per-message token accounting.
//
// Binary: fHxyBOR9qvy.TRepBeaLEAWm, vaddr 0x2883260, size 0x20. VERIFIED.
type Usage struct {
	InputTokens              int `json:"input_tokens"`                          // +0x00
	OutputTokens             int `json:"output_tokens"`                         // +0x08
	CacheCreationInputTokens int `json:"cache_creation_input_tokens,omitempty"` // +0x10
	CacheReadInputTokens     int `json:"cache_read_input_tokens,omitempty"`     // +0x18
}

// AssistantMessage represents a synthetic assistant message.
//
// Binary: fHxyBOR9qvy.Labalu1jJu, vaddr 0x28a9c00, size 0x68. VERIFIED.
type AssistantMessage struct {
	Role       string         `json:"role"`        // +0x00
	Model      string         `json:"model"`       // +0x10
	Content    []ContentBlock `json:"content"`     // +0x20
	StopReason string         `json:"stop_reason"` // +0x38
	Usage      Usage          `json:"usage"`       // +0x48
}

// DiagLogEntry is a single diagnostic log entry for forwarding.
// Used in slices.SortFunc (the binary has pdqsort/insertionSort specializations
// for this type). Both fields are untagged: this type is sorted and inspected
// in-process, never serialized directly.
//
// Binary: fHxyBOR9qvy.Ur1ssf, vaddr 0x27c1f60, size 0x20. VERIFIED.
type DiagLogEntry struct {
	Timestamp time.Time              // +0x00
	Fields    map[string]interface{} // +0x18
}

// SessionActivityLog is the structured activity-log entry posted to the
// ingress API. Distinct from DiagLogEntry, which is the in-process form.
//
// Binary: fHxyBOR9qvy.C55iIMbPNU, vaddr 0x28a9d00, size 0x50. VERIFIED.
type SessionActivityLog struct {
	Level     LogLevel               `json:"level"`     // +0x00
	Category  LogCategory            `json:"category"`  // +0x10
	Content   string                 `json:"content"`   // +0x20
	Timestamp time.Time              `json:"timestamp"` // +0x30
	Extra     map[string]interface{} `json:"extra"`     // +0x48
}

// LogLevel is the severity of a SessionActivityLog entry.
//
// TODO(re): underlying type is a named string (fHxyBOR9qvy.MAwWSLi5NPWF);
// the constant set is garble -literals encrypted and was not recovered.
type LogLevel string

// SessionIngressEvent is the top-level event posted to the session ingress API.
//
// This is Claude Code's stream-json envelope, not a simple {type,id,data}
// wrapper: the result-event metadata is carried inline on the envelope rather
// than nested in Data. Every field below is read from the binary's own type
// metadata, so the tags and offsets are exact.
//
// Binary: fHxyBOR9qvy.AIGJ5cph, vaddr 0x28eaa00, size 0xc0, 16 fields.
// VERIFIED. Byte-for-byte identical in the previous binary
// (viRrDTePbcGS.J4sedR @ 0x247a760), so this wire contract did not change.
type SessionIngressEvent struct {
	Type string `json:"type"` // +0x00
	// UUID, not "id" -- the earlier reconstruction had the tag wrong.
	UUID              string             `json:"uuid"`                         // +0x10
	Data              EventData          `json:"data,omitempty"`               // +0x20
	Message           MessageContent     `json:"message,omitempty"`            // +0x30
	ParentToolUseID   *string            `json:"parent_tool_use_id,omitempty"` // +0x40
	IsAPIErrorMessage *bool              `json:"isApiErrorMessage,omitempty"`  // +0x48
	Subtype           *string            `json:"subtype,omitempty"`            // +0x50
	IsError           *bool              `json:"is_error,omitempty"`           // +0x58
	DurationMs        *int               `json:"duration_ms,omitempty"`        // +0x60
	DurationAPIMs     *int               `json:"duration_api_ms,omitempty"`    // +0x68
	NumTurns          *int               `json:"num_turns,omitempty"`          // +0x70
	TotalCostUSD      *float64           `json:"total_cost_usd,omitempty"`     // +0x78
	Errors            []string           `json:"errors,omitempty"`             // +0x80
	ModelUsage        ModelUsage         `json:"modelUsage,omitempty"`         // +0x98
	PermissionDenials []PermissionDenial `json:"permission_denials,omitempty"` // +0xa0
	Usage             *Usage             `json:"usage,omitempty"`              // +0xb8
}

// ModelUsage is the per-model usage breakdown carried on a result event.
//
// TODO(re): named type fHxyBOR9qvy.CJFLueFJZ at +0x98 of the envelope,
// occupying one word. Its definition was not recovered; a map keyed by model
// name is the obvious shape but is NOT confirmed.
type ModelUsage interface{}

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

// REMOVED(0b86a2a0): EnvManagerLogEventData, NewEnvManagerLogEvent and
// NewEnvManagerLogEventData used to live here. They were not reconstructions of
// anything in the binary -- they were invented, and carried fabricated binary
// addresses (0x831620 / 0x831740) that lend them unearned authority.
//
// The evidence against them, checked against both the current binary
// (0b86a2a0) and the previous one (495ea204):
//
//   - The literal "env_manager_log" occurs in neither binary, and in neither
//     binary's decrypted-literal core dump. An event type that is never named
//     cannot be constructed.
//   - No struct in either binary carries the claimed tag set
//     {message, level, source, timestamp, nanos, fields}. The tag json:"source"
//     does not appear on any of the 3014 struct types in the new binary at all.
//   - The claimed SessionIngressEvent shape they built ({type, id, data}) is
//     also wrong: the real envelope has 16 fields and its identifier tag is
//     "uuid", not "id" (see SessionIngressEvent above).
//
// Diagnostic logs are carried by DiagLogEntry, whose shape *is* verified. If a
// future binary reintroduces an env-manager log event, reconstruct it from
// type metadata rather than restoring this code.

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
	id, err := uuid.NewRandom()
	if err != nil {
		panic(err)
	}

	return &SessionIngressEvent{
		Type: "assistant",
		UUID: id.String(),
		Message: AssistantMessage{
			// TODO(re): the role literal is garble -literals encrypted and was
			// not recovered; "assistant" is the only value consistent with the
			// envelope type but is NOT read from the binary.
			Role:    "assistant",
			Model:   model,
			Content: []ContentBlock{{Type: "text", Text: text}},
		},
	}
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
	id, err := uuid.NewRandom()
	if err != nil {
		panic(err)
	}

	isError := isFatal != nil
	event := &SessionIngressEvent{
		Type:    "result",
		UUID:    id.String(),
		IsError: &isError,
	}

	// TODO(re): the binary populates more of the envelope here -- the result
	// metadata now lives inline on SessionIngressEvent (subtype, duration_ms,
	// duration_api_ms, num_turns, total_cost_usd, errors, modelUsage,
	// permission_denials, usage) rather than in a nested payload, so this
	// constructor is incomplete. The earlier reconstruction discarded every
	// intermediate into `_ =` sinks, which hid that gap rather than recording
	// it. Populating it needs the field-by-field disassembly of the constructor,
	// which has not been done.
	_ = sessionError
	_ = exitCode

	return event
}
