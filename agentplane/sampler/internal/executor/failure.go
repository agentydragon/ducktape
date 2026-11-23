package executor

import (
	"encoding/json"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/executor/transport"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/outputbuffer"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/tooloutput"
)

// executionFailureRecord keeps native observations typed until they reach a storage or
// model-facing serialization boundary. RemoteOutcome describes the affected process; a
// RequestOutcome describes admission of this particular RPC and may differ for process/read.
type executionFailureRecord struct {
	message               string
	method                transport.Method
	phase                 string
	intentID              string
	processID             string
	executionSessionID    string
	afterSequence         *uint64
	remoteOutcome         sessionstore.NestedOutcome
	requestOutcome        *sessionstore.NestedOutcome
	automaticReplay       bool
	environmentState      string
	currentIncarnationUID string
	transportError        string
	output                *outputbuffer.Buffer
}

// executionFailureEvidence is a JSON boundary DTO. Its pointers retain the distinction
// between an omitted observation and an explicitly observed zero cursor or empty buffer.
type executionFailureEvidence struct {
	Status                sessionstore.IntentResultStatus `json:"status"`
	Message               string                          `json:"message,omitempty"`
	Method                transport.Method                `json:"method"`
	Phase                 string                          `json:"phase,omitempty"`
	IntentID              string                          `json:"intent_id,omitempty"`
	ProcessID             string                          `json:"process_id,omitempty"`
	ExecutionSessionID    string                          `json:"execution_session_id,omitempty"`
	AfterSeq              *uint64                         `json:"after_seq,omitempty"`
	RemoteOutcome         sessionstore.NestedOutcome      `json:"remote_outcome"`
	RequestOutcome        *sessionstore.NestedOutcome     `json:"request_outcome,omitempty"`
	AutomaticReplay       bool                            `json:"automatic_replay"`
	EnvironmentState      string                          `json:"environment_state,omitempty"`
	CurrentIncarnationUID string                          `json:"current_incarnation_uid,omitempty"`
	TransportError        string                          `json:"transport_error,omitempty"`
	BufferedOutput        *string                         `json:"buffered_output,omitempty"`
	BufferedOutputBase64  *[]byte                         `json:"buffered_output_base64,omitempty"`
	BufferedBytes         *int                            `json:"buffered_bytes,omitempty"`
	OmittedBytes          *int                            `json:"omitted_bytes,omitempty"`
	ObservedOutput        *observedProcessOutput          `json:"observed_output,omitempty"`
}

func (f executionFailureRecord) intentEvidence() executionFailureEvidence {
	return f.evidence(false)
}

func (f executionFailureRecord) toolEvidence() executionFailureEvidence {
	return f.evidence(true)
}

func (f executionFailureRecord) evidence(includeMessage bool) executionFailureEvidence {
	evidence := executionFailureEvidence{
		Status: sessionstore.IntentResultAbandoned, Method: f.method, Phase: f.phase,
		IntentID: f.intentID, ProcessID: f.processID, ExecutionSessionID: f.executionSessionID,
		AfterSeq: f.afterSequence, RemoteOutcome: f.remoteOutcome, RequestOutcome: f.requestOutcome,
		AutomaticReplay: f.automaticReplay, CurrentIncarnationUID: f.currentIncarnationUID,
		TransportError: f.transportError,
	}
	if includeMessage {
		evidence.Message = f.message
	}
	evidence.EnvironmentState = f.environmentState
	if f.output != nil {
		buffered := f.output.RetainedBytes()
		text := tooloutput.LossyUTF8(buffered)
		base64 := make([]byte, len(buffered))
		copy(base64, buffered)
		bufferedBytes, omittedBytes := len(buffered), f.output.OmittedBytes()
		evidence.BufferedOutput = &text
		evidence.BufferedOutputBase64 = &base64
		evidence.BufferedBytes = &bufferedBytes
		evidence.OmittedBytes = &omittedBytes
		if includeMessage && f.method == transport.MethodProcessRead && f.output.TotalBytes() > 0 {
			observed := observedOutputEvidence(f.output)
			evidence.ObservedOutput = &observed
		}
	}
	return evidence
}

// The pinned code-mode host accepts only a string for a rejected nested promise. Keep small
// observations machine-readable there; full output and cursors remain in typed boundary evidence.
type observedProcessOutput struct {
	Text                string `json:"text"`
	Encoding            string `json:"encoding"`
	RetainedBytes       int    `json:"retained_bytes"`
	OmittedBytes        int    `json:"omitted_bytes"`
	DisplayOmittedBytes int    `json:"display_omitted_bytes"`
	ShownHeadBytes      int    `json:"shown_head_bytes"`
	ShownTailBytes      int    `json:"shown_tail_bytes"`
	Truncated           bool   `json:"truncated"`
}

type observedProcessFailure struct {
	Message               string                      `json:"message"`
	Method                transport.Method            `json:"method"`
	Phase                 string                      `json:"phase,omitempty"`
	IntentID              string                      `json:"intent_id,omitempty"`
	ProcessID             string                      `json:"process_id,omitempty"`
	ExecutionSessionID    string                      `json:"execution_session_id,omitempty"`
	AfterSeq              *uint64                     `json:"after_seq,omitempty"`
	RemoteOutcome         sessionstore.NestedOutcome  `json:"remote_outcome"`
	RequestOutcome        *sessionstore.NestedOutcome `json:"request_outcome,omitempty"`
	AutomaticReplay       bool                        `json:"automatic_replay"`
	EnvironmentState      string                      `json:"environment_state,omitempty"`
	CurrentIncarnationUID string                      `json:"current_incarnation_uid,omitempty"`
	ObservedOutput        *observedProcessOutput      `json:"observed_output,omitempty"`
}

func executionFailureMessage(f executionFailureRecord) (string, error) {
	if f.message == "" {
		f.message = "execution request failed before dispatch"
	}
	failure := observedProcessFailure{
		Message: f.message, Method: f.method, Phase: f.phase, IntentID: f.intentID,
		ProcessID: f.processID, ExecutionSessionID: f.executionSessionID, AfterSeq: f.afterSequence,
		RemoteOutcome: f.remoteOutcome, RequestOutcome: f.requestOutcome,
		AutomaticReplay: f.automaticReplay, CurrentIncarnationUID: f.currentIncarnationUID,
	}
	failure.EnvironmentState = f.environmentState
	if f.method == transport.MethodProcessRead && f.output != nil && f.output.TotalBytes() > 0 {
		observed := observedOutputEvidence(f.output)
		failure.ObservedOutput = &observed
	}
	if failure.ObservedOutput == nil && failure.EnvironmentState == "" && failure.CurrentIncarnationUID == "" {
		return failure.Message, nil
	}
	encoded, err := json.Marshal(failure)
	return string(encoded), err
}

// executionFailureProjection belongs to the model-tool boundary. Raw intent evidence
// stays in transport; this projection also carries the process settlement facts.
func executionFailureProjection(failure *transport.Failure) executionFailureRecord {
	record := executionFailureRecord{
		message: failure.Error(), method: failure.Method, phase: failure.Phase,
		intentID: failure.IntentID, processID: failure.ProcessID,
		executionSessionID: failure.ExecutionSessionID, afterSequence: failure.AfterSequence,
		remoteOutcome: nestedOutcome(failure.RemoteOutcome), requestOutcome: nestedOutcomePointer(failure.RequestOutcome),
		automaticReplay: failure.AutomaticReplay, environmentState: failure.EnvironmentState,
		currentIncarnationUID: failure.CurrentIncarnationUID, transportError: failure.TransportError,
		output: failure.Output,
	}
	switch failure.Kind {
	case transport.FailureTransport:
		record.remoteOutcome = sessionstore.NestedUnknown
		record.transportError = failure.Cause.Error()
	case transport.FailureNotDispatched:
		record.phase = "connection"
		record.transportError = failure.Cause.Error()
		requestOutcome := sessionstore.NestedNotDispatched
		record.requestOutcome = &requestOutcome
	}
	return record
}

func observedOutputEvidence(output *outputbuffer.Buffer) observedProcessOutput {
	retained := output.RetainedBytes()
	const shownBytes = 8 * 1024
	head, tail := retained, []byte(nil)
	if len(retained) > shownBytes {
		head = retained[:shownBytes/2]
		tail = retained[len(retained)-shownBytes/2:]
	}
	displayOmitted := len(retained) - len(head) - len(tail)
	text := tooloutput.LossyUTF8(head)
	if displayOmitted > 0 || output.OmittedBytes() > 0 {
		text += fmt.Sprintf("\n... %d retained bytes not shown; %d earlier bytes omitted ...\n", displayOmitted, output.OmittedBytes())
	}
	text += tooloutput.LossyUTF8(tail)
	return observedProcessOutput{
		Text: text, Encoding: "utf8_lossy", RetainedBytes: len(retained), OmittedBytes: output.OmittedBytes(),
		DisplayOmittedBytes: displayOmitted, ShownHeadBytes: len(head), ShownTailBytes: len(tail),
		Truncated: displayOmitted > 0 || output.OmittedBytes() > 0,
	}
}
