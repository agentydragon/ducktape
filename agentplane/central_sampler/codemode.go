package centralsampler

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
	"unicode"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/codemode"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/promptimage"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/tooloutput"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
)

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
	source, err := parseExecSource(call.Input)
	if err != nil {
		return preparedCall{}, &toolError{err.Error()}
	}
	return r.prepareExecute(call.CallID, source)
}

func (r *CodeMode) prepareExecute(callID string, source execSource) (preparedCall, error) {
	yieldMS := defaultExecYieldTimeMS
	if source.yieldTimeMS != nil {
		yieldMS = *source.yieldTimeMS
	}
	hostDefinitions, err := hostToolDefinitions(toolDefinitions())
	if err != nil {
		return preparedCall{}, err
	}
	definitions, err := json.Marshal(hostDefinitions)
	if err != nil {
		return preparedCall{}, err
	}
	request := codemode.ExecuteRequest{ToolCallID: callID, Source: source.code, EnabledTools: definitions, YieldTimeMS: yieldMS, MaxOutputTokens: source.maxOutputTokens}
	return preparedCall{
		operation: codemode.ExecuteOperation{SessionOperation: codemode.SessionOperation{Method: "session/execute", SessionID: r.ID()}, Request: request},
		execute:   true, maxTokens: source.maxOutputTokens,
	}, nil
}

type jsonExecInput struct {
	Code            *string `json:"code,omitempty"`
	CellID          *string `json:"cell_id,omitempty"`
	YieldTimeMS     *uint64 `json:"yield_time_ms,omitempty" jsonschema:"minimum=0"`
	MaxOutputTokens *uint64 `json:"max_output_tokens,omitempty" jsonschema:"minimum=0"`
	Terminate       *bool   `json:"terminate,omitempty"`
}

// JSON exec uses the same host operations and journal as the native freeform contract.
// Resuming through exec keeps the compatibility surface to one function.
func (r *CodeMode) prepareJSONCall(ctx context.Context, call protocol.ToolInvocation) (preparedCall, error) {
	var input jsonExecInput
	if err := decodeToolInput([]byte(call.Input), &input); err != nil {
		return preparedCall{}, &toolError{err.Error()}
	}
	if (input.Code == nil) == (input.CellID == nil) {
		return preparedCall{}, &toolError{"exec requires exactly one of code or cell_id"}
	}
	if input.CellID != nil {
		terminate := input.Terminate != nil && *input.Terminate
		arguments, err := resolveWaitArguments(waitInput{
			CellID: *input.CellID, YieldTimeMS: input.YieldTimeMS,
			MaxTokens: input.MaxOutputTokens, Terminate: terminate,
		})
		if err != nil {
			return preparedCall{}, &toolError{err.Error()}
		}
		return r.prepareWaitArguments(ctx, arguments)
	}
	if input.Terminate != nil {
		return preparedCall{}, &toolError{"terminate requires cell_id"}
	}
	if strings.TrimSpace(*input.Code) == "" {
		return preparedCall{}, &toolError{"exec requires non-empty code"}
	}
	yieldMS, err := safeIntegerArgument("yield_time_ms", input.YieldTimeMS)
	if err != nil {
		return preparedCall{}, &toolError{err.Error()}
	}
	maxTokens, err := safeIntegerArgument("max_output_tokens", input.MaxOutputTokens)
	if err != nil {
		return preparedCall{}, &toolError{err.Error()}
	}
	return r.prepareExecute(call.CallID, execSource{code: *input.Code, yieldTimeMS: yieldMS, maxOutputTokens: maxTokens})
}

type waitArguments struct {
	cellID      string
	yieldTimeMS int
	// maxTokens is nil when the model omits it.
	maxTokens *int
	terminate bool
}

// parseWaitArguments applies Codex rust-v0.162.1 core/src/tools/code_mode/wait_handler.rs
// ExecWaitArgs defaults.
func parseWaitArguments(input string) (waitArguments, error) {
	var arguments waitInput
	if err := decodeToolInput([]byte(input), &arguments); err != nil {
		return waitArguments{}, err
	}
	return resolveWaitArguments(arguments)
}

func resolveWaitArguments(arguments waitInput) (waitArguments, error) {
	if arguments.CellID == "" {
		return waitArguments{}, errors.New("wait requires a string cell_id")
	}
	yieldMS, err := safeIntegerArgument("yield_time_ms", arguments.YieldTimeMS)
	if err != nil {
		return waitArguments{}, err
	}
	maxTokens, err := safeIntegerArgument("max_tokens", arguments.MaxTokens)
	if err != nil {
		return waitArguments{}, err
	}
	parsed := waitArguments{cellID: arguments.CellID, yieldTimeMS: defaultWaitYieldTimeMS, maxTokens: maxTokens, terminate: arguments.Terminate}
	if yieldMS != nil {
		parsed.yieldTimeMS = *yieldMS
	}
	return parsed, nil
}

func (r *CodeMode) prepareWait(ctx context.Context, input string) (preparedCall, error) {
	arguments, err := parseWaitArguments(input)
	if err != nil {
		return preparedCall{}, &toolError{err.Error()}
	}
	return r.prepareWaitArguments(ctx, arguments)
}

func (r *CodeMode) prepareWaitArguments(ctx context.Context, arguments waitArguments) (preparedCall, error) {
	cellID := arguments.cellID
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
	if arguments.terminate {
		session.Method = "session/terminate"
		return preparedCall{operation: codemode.TerminateOperation{SessionOperation: session, CellID: nativeID}, maxTokens: arguments.maxTokens}, nil
	}
	request := codemode.WaitRequest{CellID: nativeID, YieldTimeMS: arguments.yieldTimeMS}
	return preparedCall{operation: codemode.WaitOperation{SessionOperation: session, Request: request}, maxTokens: arguments.maxTokens}, nil
}

// safeIntegerArgument bounds an unsigned tool argument to the JavaScript safe integer range, as
// Codex does, so it fits the host protocol's integers.
func safeIntegerArgument(name string, value *uint64) (*int, error) {
	if value == nil {
		return nil, nil
	}
	if *value > maxJSSafeInteger {
		return nil, fmt.Errorf("%s must be a non-negative safe integer", name)
	}
	converted := int(*value)
	return &converted, nil
}

type execSource struct {
	code            string
	yieldTimeMS     *int
	maxOutputTokens *int
}

// parseExecSource ports Codex rust-v0.162.1 code-mode-protocol/src/description.rs
// parse_exec_source, including its model-facing error text.
func parseExecSource(input string) (execSource, error) {
	const supported = "supported fields `yield_time_ms` and `max_output_tokens`"
	if strings.TrimSpace(input) == "" {
		return execSource{}, errors.New("exec expects raw JavaScript source text (non-empty). Provide JS only, optionally with first-line `// @exec: {\"yield_time_ms\": 10000, \"max_output_tokens\": 1000}`.")
	}
	firstLine, rest, _ := strings.Cut(input, "\n")
	pragma, found := strings.CutPrefix(strings.TrimLeftFunc(firstLine, unicode.IsSpace), execPragmaPrefix)
	if !found {
		return execSource{code: input}, nil
	}
	if strings.TrimSpace(rest) == "" {
		return execSource{}, errors.New("exec pragma must be followed by JavaScript source on subsequent lines")
	}
	directive := strings.TrimSpace(pragma)
	if directive == "" {
		return execSource{}, errors.New("exec pragma must be a JSON object with " + supported)
	}
	var value any
	if err := json.Unmarshal([]byte(directive), &value); err != nil {
		return execSource{}, fmt.Errorf("exec pragma must be valid JSON with %s: %w", supported, err)
	}
	fields, ok := value.(map[string]any)
	if !ok {
		return execSource{}, errors.New("exec pragma must be a JSON object with " + supported)
	}
	for key := range fields {
		if key != "yield_time_ms" && key != "max_output_tokens" {
			return execSource{}, fmt.Errorf("exec pragma only supports `yield_time_ms` and `max_output_tokens`; got `%s`", key)
		}
	}
	var arguments execPragma
	if err := json.Unmarshal([]byte(directive), &arguments); err != nil {
		return execSource{}, fmt.Errorf("exec pragma fields `yield_time_ms` and `max_output_tokens` must be non-negative safe integers: %w", err)
	}
	yieldMS, err := safeIntegerArgument("exec pragma field `yield_time_ms`", arguments.YieldTimeMS)
	if err != nil {
		return execSource{}, err
	}
	maxOutputTokens, err := safeIntegerArgument("exec pragma field `max_output_tokens`", arguments.MaxOutputTokens)
	if err != nil {
		return execSource{}, err
	}
	return execSource{code: rest, yieldTimeMS: yieldMS, maxOutputTokens: maxOutputTokens}, nil
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
