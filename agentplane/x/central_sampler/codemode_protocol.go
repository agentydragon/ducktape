package centralsampler

import (
	"encoding/json"
	"errors"
	"fmt"
)

// Codex rust-v0.162.1 code-mode-protocol/src/host/{message,payload}.rs owns protocol V1.
type codeFrameType string

const (
	codeReady           codeFrameType = "connection/ready"
	codeDelegateRequest codeFrameType = "delegate/request"
	codeDelegateCancel  codeFrameType = "delegate/cancel"
	codeResponse        codeFrameType = "operation/response"
	codeInitialResponse codeFrameType = "execute/initialResponse"
	codeCellClosed      codeFrameType = "cell/closed"
)

type codeFrame struct {
	Type            codeFrameType   `json:"type"`
	ID              *int64          `json:"id"`
	SessionID       string          `json:"sessionId"`
	CellID          string          `json:"cellId"`
	SelectedVersion int             `json:"selectedVersion"`
	Capabilities    []string        `json:"capabilities"`
	Request         *codeDelegate   `json:"request"`
	Result          *codeWireResult `json:"result"`
}

type codeWireResult struct {
	Status  string          `json:"status"`
	Value   json.RawMessage `json:"value"`
	Message string          `json:"message"`
}

type codeReply struct {
	Result    *codeWireResult
	Err       error
	Journaled <-chan error
}

type codeHello struct {
	Type                 string   `json:"type"`
	SupportedVersions    []int    `json:"supportedVersions"`
	RequiredCapabilities []string `json:"requiredCapabilities"`
	OptionalCapabilities []string `json:"optionalCapabilities"`
}

type codeCancelRequest struct {
	Type string `json:"type"`
	ID   int64  `json:"id"`
}

type (
	codeOperation        interface{ method() string }
	codeSessionOperation struct {
		Method    string `json:"method"`
		SessionID string `json:"sessionId"`
	}
)

func (o codeSessionOperation) method() string { return o.Method }

type codeExecuteOperation struct {
	codeSessionOperation
	Request codeExecuteRequest `json:"request"`
}

type codeExecuteRequest struct {
	ToolCallID      string          `json:"tool_call_id"`
	Source          string          `json:"source"`
	EnabledTools    json.RawMessage `json:"enabled_tools"`
	YieldTimeMS     int             `json:"yield_time_ms"`
	MaxOutputTokens int             `json:"max_output_tokens"`
}

type codeWaitOperation struct {
	codeSessionOperation
	Request codeWaitRequest `json:"request"`
}

type codeWaitRequest struct {
	CellID      string `json:"cell_id"`
	YieldTimeMS int    `json:"yield_time_ms"`
}

type codeTerminateOperation struct {
	codeSessionOperation
	CellID string `json:"cellId"`
}

type codeOperationFrame struct {
	Type    string        `json:"type"`
	ID      int64         `json:"id"`
	Request codeOperation `json:"request"`
}

type codeToolName struct {
	Name      string  `json:"name"`
	Namespace *string `json:"namespace"`
}

type codeDelegate struct {
	Type       string             `json:"type"`
	Invocation codeToolInvocation `json:"invocation"`
	CallID     string             `json:"callId"`
	CellID     string             `json:"cellId"`
	Text       string             `json:"text"`
}

type codeToolInvocation struct {
	CellID            string          `json:"cell_id"`
	RuntimeToolCallID string          `json:"runtime_tool_call_id"`
	ToolName          codeToolName    `json:"tool_name"`
	ToolKind          string          `json:"tool_kind"`
	Input             json.RawMessage `json:"input"`
}

type codeDelegateResponse struct {
	Type   string             `json:"type"`
	ID     int64              `json:"id"`
	Result codeDelegateResult `json:"result"`
}

type codeDelegateResult struct {
	Status  string          `json:"status"`
	Value   *codeToolResult `json:"value,omitempty"`
	Message string          `json:"message,omitempty"`
}

type codeToolResult struct {
	Type   string          `json:"type"`
	Result json.RawMessage `json:"result,omitempty"`
}

type codeHostResponse struct {
	Type      string          `json:"type"`
	SessionID string          `json:"sessionId"`
	CellID    string          `json:"cellId"`
	Outcome   json.RawMessage `json:"outcome"`
}

type codeObservationKind string

const (
	codeYielded    codeObservationKind = "Yielded"
	codeTerminated codeObservationKind = "Terminated"
	codeResult     codeObservationKind = "Result"
)

type codeCellOutput struct {
	CellID       string            `json:"cell_id"`
	ContentItems []json.RawMessage `json:"content_items"`
	ErrorText    *string           `json:"error_text"`
}

type codeObservation struct {
	Kind codeObservationKind
	Cell codeCellOutput
}

func (o *codeObservation) UnmarshalJSON(data []byte) error {
	var variants map[string]json.RawMessage
	if err := decodeProtocolObject(data, &variants); err != nil {
		return err
	}
	if len(variants) != 1 {
		return errors.New("code-mode observation must have exactly one variant")
	}
	for kind, raw := range variants {
		o.Kind = codeObservationKind(kind)
		switch o.Kind {
		case codeYielded, codeTerminated, codeResult:
		default:
			return fmt.Errorf("unsupported code-mode observation %q", kind)
		}
		if err := decodeProtocolObject(raw, &o.Cell); err != nil {
			return err
		}
	}
	if o.Cell.CellID == "" || o.Cell.ContentItems == nil {
		return errors.New("code-mode observation requires cell_id and content_items")
	}
	return nil
}

func decodeWaitObservation(raw json.RawMessage) (codeObservation, error) {
	var reply codeHostResponse
	if err := decodeProtocolObject(raw, &reply); err != nil {
		return codeObservation{}, err
	}
	if reply.Type != "wait/completed" {
		return codeObservation{}, fmt.Errorf("unexpected code-mode wait response %q", reply.Type)
	}
	var outcome map[string]json.RawMessage
	if err := decodeProtocolObject(reply.Outcome, &outcome); err != nil {
		return codeObservation{}, err
	}
	if len(outcome) != 1 {
		return codeObservation{}, errors.New("code-mode wait requires exactly one outcome")
	}
	for kind, value := range outcome {
		if kind != "LiveCell" && kind != "MissingCell" {
			return codeObservation{}, fmt.Errorf("unsupported code-mode wait outcome %q", kind)
		}
		var observation codeObservation
		if err := decodeProtocolObject(value, &observation); err != nil {
			return codeObservation{}, err
		}
		return observation, nil
	}
	panic("unreachable")
}
