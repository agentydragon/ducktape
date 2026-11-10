package centralsampler

import (
	"encoding/json"
	"fmt"
)

type executionMethod string

const (
	executorMethodInitialize       executionMethod = "initialize"
	executorMethodExecCommand      executionMethod = "exec_command"
	executorMethodApplyPatch       executionMethod = "apply_patch"
	executorMethodProcessStart     executionMethod = "process/start"
	executorMethodProcessRead      executionMethod = "process/read"
	executorMethodProcessWrite     executionMethod = "process/write"
	executorMethodProcessSignal    executionMethod = "process/signal"
	executorMethodProcessTerminate executionMethod = "process/terminate"
	executorMethodFSGetMetadata    executionMethod = "fs/getMetadata"
	executorMethodFSOpen           executionMethod = "fs/open"
	executorMethodFSReadBlock      executionMethod = "fs/readBlock"
	executorMethodFSClose          executionMethod = "fs/close"
	executorMethodSessionNotice    executionMethod = "session_notification"
)

// executionFailureRecord keeps native observations typed until they reach a storage or
// model-facing serialization boundary. RemoteOutcome describes the affected process; a
// RequestOutcome describes admission of this particular RPC and may differ for process/read.
type executionFailureRecord struct {
	message               string
	method                executionMethod
	phase                 string
	intentID              string
	processID             string
	executionSessionID    string
	afterSequence         *uint64
	remoteOutcome         NestedOutcome
	requestOutcome        *NestedOutcome
	automaticReplay       bool
	environmentState      fmt.Stringer
	currentIncarnationUID string
	transportError        string
	output                *headTailBuffer
}

type executionFailureSource interface {
	executionFailureRecord() executionFailureRecord
}

// executionFailureEvidence is a JSON boundary DTO. Its pointers retain the distinction
// between an omitted observation and an explicitly observed zero cursor or empty buffer.
type executionFailureEvidence struct {
	Status                IntentResultStatus     `json:"status"`
	Message               string                 `json:"message,omitempty"`
	Method                executionMethod        `json:"method"`
	Phase                 string                 `json:"phase,omitempty"`
	IntentID              string                 `json:"intent_id,omitempty"`
	ProcessID             string                 `json:"process_id,omitempty"`
	ExecutionSessionID    string                 `json:"execution_session_id,omitempty"`
	AfterSeq              *uint64                `json:"after_seq,omitempty"`
	RemoteOutcome         NestedOutcome          `json:"remote_outcome"`
	RequestOutcome        *NestedOutcome         `json:"request_outcome,omitempty"`
	AutomaticReplay       bool                   `json:"automatic_replay"`
	EnvironmentState      string                 `json:"environment_state,omitempty"`
	CurrentIncarnationUID string                 `json:"current_incarnation_uid,omitempty"`
	TransportError        string                 `json:"transport_error,omitempty"`
	BufferedOutput        *string                `json:"buffered_output,omitempty"`
	BufferedOutputBase64  *[]byte                `json:"buffered_output_base64,omitempty"`
	BufferedBytes         *int                   `json:"buffered_bytes,omitempty"`
	OmittedBytes          *int                   `json:"omitted_bytes,omitempty"`
	ObservedOutput        *observedProcessOutput `json:"observed_output,omitempty"`
}

func (f executionFailureRecord) intentEvidence() executionFailureEvidence {
	return f.evidence(false)
}

func (f executionFailureRecord) toolEvidence() executionFailureEvidence {
	return f.evidence(true)
}

func (f executionFailureRecord) evidence(includeMessage bool) executionFailureEvidence {
	evidence := executionFailureEvidence{
		Status: IntentResultAbandoned, Method: f.method, Phase: f.phase,
		IntentID: f.intentID, ProcessID: f.processID, ExecutionSessionID: f.executionSessionID,
		AfterSeq: f.afterSequence, RemoteOutcome: f.remoteOutcome, RequestOutcome: f.requestOutcome,
		AutomaticReplay: f.automaticReplay, CurrentIncarnationUID: f.currentIncarnationUID,
		TransportError: f.transportError,
	}
	if includeMessage {
		evidence.Message = f.message
	}
	if f.environmentState != nil {
		evidence.EnvironmentState = f.environmentState.String()
	}
	if f.output != nil {
		buffered := f.output.retained()
		text := lossyUTF8(buffered)
		base64 := make([]byte, len(buffered))
		copy(base64, buffered)
		bufferedBytes, omittedBytes := len(buffered), f.output.omitted
		evidence.BufferedOutput = &text
		evidence.BufferedOutputBase64 = &base64
		evidence.BufferedBytes = &bufferedBytes
		evidence.OmittedBytes = &omittedBytes
		if includeMessage && f.method == executorMethodProcessRead && f.output.totalBytes() > 0 {
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
	Message               string                 `json:"message"`
	Method                executionMethod        `json:"method"`
	Phase                 string                 `json:"phase,omitempty"`
	IntentID              string                 `json:"intent_id,omitempty"`
	ProcessID             string                 `json:"process_id,omitempty"`
	ExecutionSessionID    string                 `json:"execution_session_id,omitempty"`
	AfterSeq              *uint64                `json:"after_seq,omitempty"`
	RemoteOutcome         NestedOutcome          `json:"remote_outcome"`
	RequestOutcome        *NestedOutcome         `json:"request_outcome,omitempty"`
	AutomaticReplay       bool                   `json:"automatic_replay"`
	EnvironmentState      string                 `json:"environment_state,omitempty"`
	CurrentIncarnationUID string                 `json:"current_incarnation_uid,omitempty"`
	ObservedOutput        *observedProcessOutput `json:"observed_output,omitempty"`
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
	if f.environmentState != nil {
		failure.EnvironmentState = f.environmentState.String()
	}
	if f.method == executorMethodProcessRead && f.output != nil && f.output.totalBytes() > 0 {
		observed := observedOutputEvidence(f.output)
		failure.ObservedOutput = &observed
	}
	if failure.ObservedOutput == nil && failure.EnvironmentState == "" && failure.CurrentIncarnationUID == "" {
		return failure.Message, nil
	}
	encoded, err := json.Marshal(failure)
	return string(encoded), err
}

func observedProcessFailureMessage(transport *executorTransportError) (string, error) {
	return executionFailureMessage(executionFailureRecord{
		message: transport.Error(), method: transport.method, phase: transport.phase,
		intentID: transport.intentID, processID: transport.processID,
		executionSessionID: transport.retainedSession, afterSequence: transport.afterSequence,
		remoteOutcome: NestedUnknown, automaticReplay: false, output: transport.output,
	})
}

func (e *executorNotDispatchedError) executionFailureRecord(remoteOutcome NestedOutcome) executionFailureRecord {
	requestOutcome := NestedNotDispatched
	return executionFailureRecord{
		message: e.Error(), method: e.method, phase: "connection", processID: e.processID,
		executionSessionID: e.retainedSession, afterSequence: e.afterSequence,
		remoteOutcome: remoteOutcome, requestOutcome: &requestOutcome, automaticReplay: false,
		transportError: e.cause.Error(), output: e.output,
	}
}

func observedOutputEvidence(output *headTailBuffer) observedProcessOutput {
	retained := output.retained()
	const shownBytes = 8 * 1024
	head, tail := retained, []byte(nil)
	if len(retained) > shownBytes {
		head = retained[:shownBytes/2]
		tail = retained[len(retained)-shownBytes/2:]
	}
	displayOmitted := len(retained) - len(head) - len(tail)
	text := lossyUTF8(head)
	if displayOmitted > 0 || output.omitted > 0 {
		text += fmt.Sprintf("\n... %d retained bytes not shown; %d earlier bytes omitted ...\n", displayOmitted, output.omitted)
	}
	text += lossyUTF8(tail)
	return observedProcessOutput{
		Text: text, Encoding: "utf8_lossy", RetainedBytes: len(retained), OmittedBytes: output.omitted,
		DisplayOmittedBytes: displayOmitted, ShownHeadBytes: len(head), ShownTailBytes: len(tail),
		Truncated: displayOmitted > 0 || output.omitted > 0,
	}
}
