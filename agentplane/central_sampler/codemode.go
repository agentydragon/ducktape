package centralsampler

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/codemode"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/promptimage"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/toolcontract"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/tooloutput"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
)

// Codex rust-v0.162.1 core/src/unified_exec/mod.rs DEFAULT_MAX_OUTPUT_TOKENS, the
// resolve_max_tokens fallback for both exec max_output_tokens and wait max_tokens.
const defaultCodeModeMaxOutputTokens = 10_000

type CodeMode struct {
	*codemode.Session
	store    *Store
	owner    Owner
	executor *Executor
}

func (r *CodeMode) Execute(ctx context.Context, call protocol.ToolInvocation) ([]protocol.ContentPart, error) {
	prepared, err := r.prepareCall(ctx, call)
	if err != nil {
		// Codex's FunctionCallError::RespondToModel returns input errors to the model;
		// they do not stop the turn or abandon unrelated live cells and processes.
		var inputError *toolError
		if errors.As(err, &inputError) {
			return []protocol.ContentPart{{Type: "input_text", Text: "Error: " + inputError.Error()}}, nil
		}
		return nil, err
	}
	started := time.Now()
	value, err := r.Request(ctx, prepared.operation, prepared.execute)
	if err != nil {
		return nil, err
	}
	var observation tooloutput.Observation
	if !prepared.execute {
		observation, err = tooloutput.DecodeWaitObservation(value)
	} else {
		err = protocol.DecodeObject(value, &observation)
	}
	if err != nil {
		return nil, err
	}
	if err := r.observe(ctx, observation); err != nil {
		return nil, err
	}
	wallTime := time.Since(started)
	if duration := observation.Cell.CodeModeHostDurationNS; duration != nil {
		wallTime = time.Duration(*duration)
	}
	maxTokens := defaultCodeModeMaxOutputTokens
	if prepared.maxTokens != nil {
		maxTokens = *prepared.maxTokens
	}
	output, err := tooloutput.RenderCellOutput(observation, r.CellHandle(observation.Cell.CellID), maxTokens, wallTime)
	if err != nil {
		return nil, err
	}
	// Upstream prepares images when the truncated output enters history.
	return promptimage.PrepareOutputImages(output)
}

type preparedCall struct {
	operation codemode.Operation
	execute   bool
	// maxTokens is the model's output token budget; nil uses the Codex default.
	maxTokens *int
}

func (r *CodeMode) prepareCall(ctx context.Context, call protocol.ToolInvocation) (preparedCall, error) {
	if call.Name == "exec" && call.Encoding == protocol.ExecEncodingJSON {
		return r.prepareJSONCall(ctx, call)
	}
	if call.Name == "wait" && call.Encoding == protocol.ExecEncodingJSON {
		return r.prepareWait(ctx, call.Input)
	}
	if call.Name != "exec" || call.Encoding != protocol.ExecEncodingFreeform {
		return preparedCall{}, &toolError{fmt.Sprintf("unsupported model tool %q (%s)", call.Name, call.Encoding)}
	}
	source, err := toolcontract.ParseExecSource(call.Input)
	if err != nil {
		return preparedCall{}, &toolError{err.Error()}
	}
	return r.prepareExecute(call.CallID, source)
}

func (r *CodeMode) prepareExecute(callID string, source toolcontract.ExecSource) (preparedCall, error) {
	hostDefinitions, err := toolcontract.HostToolDefinitions(toolDefinitions())
	if err != nil {
		return preparedCall{}, err
	}
	definitions, err := json.Marshal(hostDefinitions)
	if err != nil {
		return preparedCall{}, err
	}
	request := codemode.ExecuteRequest{ToolCallID: callID, Source: source.Code, EnabledTools: definitions, YieldTimeMS: source.YieldTimeMS, MaxOutputTokens: source.MaxOutputTokens}
	return preparedCall{
		operation: codemode.ExecuteOperation{SessionOperation: codemode.SessionOperation{Method: "session/execute", SessionID: r.ID()}, Request: request},
		execute:   true, maxTokens: source.MaxOutputTokens,
	}, nil
}

// JSON exec uses the same host operations and journal as the native freeform contract.
// Resuming through exec keeps the compatibility surface to one function.
func (r *CodeMode) prepareJSONCall(ctx context.Context, call protocol.ToolInvocation) (preparedCall, error) {
	parsed, err := toolcontract.ParseJSONExecInput(call.Input)
	if err != nil {
		return preparedCall{}, &toolError{err.Error()}
	}
	if parsed.Wait != nil {
		return r.prepareWaitArguments(ctx, *parsed.Wait)
	}
	return r.prepareExecute(call.CallID, *parsed.Source)
}

func (r *CodeMode) prepareWait(ctx context.Context, input string) (preparedCall, error) {
	arguments, err := toolcontract.ParseWaitArguments(input)
	if err != nil {
		return preparedCall{}, &toolError{err.Error()}
	}
	return r.prepareWaitArguments(ctx, arguments)
}

func (r *CodeMode) prepareWaitArguments(ctx context.Context, arguments toolcontract.WaitArguments) (preparedCall, error) {
	cellID := arguments.CellID
	state, err := r.store.ExecutionState(ctx, r.owner)
	if err != nil {
		return preparedCall{}, err
	}
	known := false
	for _, cell := range state.Cells {
		if cell.ID == cellID && cell.Status == protocol.CellLive {
			known = true
			break
		}
	}
	if !known {
		return preparedCall{}, &toolError{"wait requires a known live cell_id; process session_id is not accepted"}
	}
	nativeID, err := r.NativeCell(cellID)
	if err != nil {
		return preparedCall{}, &toolError{err.Error()}
	}
	session := codemode.SessionOperation{Method: "session/wait", SessionID: r.ID()}
	if arguments.Terminate {
		session.Method = "session/terminate"
		return preparedCall{operation: codemode.TerminateOperation{SessionOperation: session, CellID: nativeID}, maxTokens: arguments.MaxTokens}, nil
	}
	request := codemode.WaitRequest{CellID: nativeID, YieldTimeMS: arguments.YieldTimeMS}
	return preparedCall{operation: codemode.WaitOperation{SessionOperation: session, Request: request}, maxTokens: arguments.MaxTokens}, nil
}

// observe records the observed cell's lifecycle status.
func (r *CodeMode) observe(ctx context.Context, observation tooloutput.Observation) error {
	status := protocol.CellCompleted
	switch observation.Kind {
	case tooloutput.ObservationYielded:
		status = protocol.CellLive
	case tooloutput.ObservationTerminated:
		status = protocol.CellTerminated
	case tooloutput.ObservationResult:
	default:
		return fmt.Errorf("unsupported cell observation %q", observation.Kind)
	}
	return r.store.CellResult(ctx, r.owner, r.CellHandle(observation.Cell.CellID), status)
}

func (r *CodeMode) Interrupt(ctx context.Context) (map[string]any, error) {
	state, err := r.store.ExecutionState(ctx, r.owner)
	if err != nil {
		return nil, err
	}
	outcomes := map[string]any{}
	for _, cell := range state.Cells {
		if !strings.HasPrefix(cell.ID, r.ID()+":") || cell.Status != protocol.CellLive {
			continue
		}
		nativeID, err := r.NativeCell(cell.ID)
		if err != nil {
			outcomes[cell.ID] = map[string]any{"error": err.Error()}
			continue
		}
		requestCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
		value, requestErr := r.Request(requestCtx, codemode.TerminateOperation{SessionOperation: codemode.SessionOperation{Method: "session/terminate", SessionID: r.ID()}, CellID: nativeID}, false)
		cancel()
		if requestErr != nil {
			outcomes[cell.ID] = map[string]any{"error": requestErr.Error()}
			continue
		}
		observation, err := tooloutput.DecodeWaitObservation(value)
		if err != nil {
			outcomes[cell.ID] = map[string]any{"error": err.Error()}
			continue
		}
		if err := r.observe(ctx, observation); err != nil {
			outcomes[cell.ID] = map[string]any{"error": err.Error()}
		} else {
			outcomes[cell.ID] = map[string]any{"stopped": observation.Kind != tooloutput.ObservationYielded}
		}
	}
	return outcomes, nil
}
