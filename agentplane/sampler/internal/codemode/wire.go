package codemode

import (
	"encoding/json"
)

// Codex rust-v0.162.1 code-mode-protocol/src/host/{message,payload}.rs owns protocol V1.
type FrameType string

const (
	Ready           FrameType = "connection/ready"
	DelegateRequest FrameType = "delegate/request"
	DelegateCancel  FrameType = "delegate/cancel"
	Response        FrameType = "operation/response"
	InitialResponse FrameType = "execute/initialResponse"
	CellClosed      FrameType = "cell/closed"
)

type Frame struct {
	Type            FrameType   `json:"type"`
	ID              *int64      `json:"id"`
	SessionID       string      `json:"sessionId"`
	CellID          string      `json:"cellId"`
	SelectedVersion int         `json:"selectedVersion"`
	Capabilities    []string    `json:"capabilities"`
	Request         *Delegate   `json:"request"`
	Result          *WireResult `json:"result"`
}

type WireResult struct {
	Status  string          `json:"status"`
	Value   json.RawMessage `json:"value"`
	Message string          `json:"message"`
}

type codeReply struct {
	Result    *WireResult
	Err       error
	Journaled <-chan error
}

type Hello struct {
	Type                 string   `json:"type"`
	SupportedVersions    []int    `json:"supportedVersions"`
	RequiredCapabilities []string `json:"requiredCapabilities"`
	OptionalCapabilities []string `json:"optionalCapabilities"`
}

// Codex rust-v0.162.1 code-mode-protocol/src/host/mod.rs YIELD_OBSERVATION_CAPABILITY.
const YieldObservationCapability = "yield-observation"

// codeRequestSignal is `operation/cancel` or `operation/yield` for an in-flight request.
type codeRequestSignal struct {
	Type string `json:"type"`
	ID   int64  `json:"id"`
}

type (
	Operation        interface{ method() string }
	SessionOperation struct {
		Method    string `json:"method"`
		SessionID string `json:"sessionId"`
	}
)

func (o SessionOperation) method() string { return o.Method }

type ExecuteOperation struct {
	SessionOperation
	Request ExecuteRequest `json:"request"`
}

type ExecuteRequest struct {
	ToolCallID      string          `json:"tool_call_id"`
	Source          string          `json:"source"`
	EnabledTools    json.RawMessage `json:"enabled_tools"`
	YieldTimeMS     int             `json:"yield_time_ms"`
	MaxOutputTokens *int            `json:"max_output_tokens"`
}

type WaitOperation struct {
	SessionOperation
	Request WaitRequest `json:"request"`
}

type WaitRequest struct {
	CellID      string `json:"cell_id"`
	YieldTimeMS int    `json:"yield_time_ms"`
}

type TerminateOperation struct {
	SessionOperation
	CellID string `json:"cellId"`
}

type codeOperationFrame struct {
	Type    string    `json:"type"`
	ID      int64     `json:"id"`
	Request Operation `json:"request"`
}

type ToolName struct {
	Name      string  `json:"name"`
	Namespace *string `json:"namespace"`
}

type Delegate struct {
	Type       string         `json:"type"`
	Invocation ToolInvocation `json:"invocation"`
	CallID     string         `json:"callId"`
	CellID     string         `json:"cellId"`
	Text       string         `json:"text"`
}

type ToolInvocation struct {
	CellID            string          `json:"cell_id"`
	RuntimeToolCallID string          `json:"runtime_tool_call_id"`
	ToolName          ToolName        `json:"tool_name"`
	ToolKind          string          `json:"tool_kind"`
	Input             json.RawMessage `json:"input"`
}

type DelegateResponse struct {
	Type   string         `json:"type"`
	ID     int64          `json:"id"`
	Result DelegateResult `json:"result"`
}

type DelegateResult struct {
	Status  string      `json:"status"`
	Value   *ToolResult `json:"value,omitempty"`
	Message string      `json:"message,omitempty"`
}

type ToolResult struct {
	Type   string          `json:"type"`
	Result json.RawMessage `json:"result,omitempty"`
}

type HostResponse struct {
	Type      string          `json:"type"`
	SessionID string          `json:"sessionId"`
	CellID    string          `json:"cellId"`
	Outcome   json.RawMessage `json:"outcome"`
}
