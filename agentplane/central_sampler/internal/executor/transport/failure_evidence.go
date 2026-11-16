package transport

import (
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/tooloutput"
)

type IntentEvidence struct {
	Status                string   `json:"status"`
	Method                Method   `json:"method"`
	Phase                 string   `json:"phase,omitempty"`
	IntentID              string   `json:"intent_id,omitempty"`
	ProcessID             string   `json:"process_id,omitempty"`
	ExecutionSessionID    string   `json:"execution_session_id,omitempty"`
	AfterSeq              *uint64  `json:"after_seq,omitempty"`
	RemoteOutcome         Outcome  `json:"remote_outcome"`
	RequestOutcome        *Outcome `json:"request_outcome,omitempty"`
	AutomaticReplay       bool     `json:"automatic_replay"`
	EnvironmentState      string   `json:"environment_state,omitempty"`
	CurrentIncarnationUID string   `json:"current_incarnation_uid,omitempty"`
	TransportError        string   `json:"transport_error,omitempty"`
	BufferedOutput        *string  `json:"buffered_output,omitempty"`
	BufferedOutputBase64  *[]byte  `json:"buffered_output_base64,omitempty"`
	BufferedBytes         *int     `json:"buffered_bytes,omitempty"`
	OmittedBytes          *int     `json:"omitted_bytes,omitempty"`
}

func (f *Failure) IntentEvidence() IntentEvidence {
	remoteOutcome := f.RemoteOutcome
	transportError := f.TransportError
	if f.Kind == FailureTransport {
		remoteOutcome = Unknown
		if f.Cause != nil {
			transportError = f.Cause.Error()
		}
	}
	evidence := IntentEvidence{
		Status: "abandoned", Method: f.Method, Phase: f.Phase, IntentID: f.IntentID,
		ProcessID: f.ProcessID, ExecutionSessionID: f.ExecutionSessionID, AfterSeq: f.AfterSequence,
		RemoteOutcome: remoteOutcome, RequestOutcome: f.RequestOutcome,
		AutomaticReplay: f.AutomaticReplay, EnvironmentState: f.EnvironmentState,
		CurrentIncarnationUID: f.CurrentIncarnationUID, TransportError: transportError,
	}
	if f.Kind == FailureEnvironment {
		evidence.RemoteOutcome = NotDispatched
	}
	if f.Output != nil {
		buffered := f.Output.RetainedBytes()
		text := tooloutput.LossyUTF8(buffered)
		base64 := make([]byte, len(buffered))
		copy(base64, buffered)
		bufferedBytes, omittedBytes := len(buffered), f.Output.OmittedBytes()
		evidence.BufferedOutput = &text
		evidence.BufferedOutputBase64 = &base64
		evidence.BufferedBytes = &bufferedBytes
		evidence.OmittedBytes = &omittedBytes
	}
	return evidence
}

func outcomePointer(outcome Outcome) *Outcome { return &outcome }
