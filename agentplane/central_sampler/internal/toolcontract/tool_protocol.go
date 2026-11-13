package toolcontract

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"unicode"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
)

// ExecSource is the source and optional budget parsed from a freeform exec call.
type ExecSource struct {
	Code            string
	YieldTimeMS     int
	MaxOutputTokens *int
}

// WaitArguments are the validated arguments for a wait or terminate operation.
type WaitArguments struct {
	CellID      string
	YieldTimeMS int
	// MaxTokens is nil when the model omits it.
	MaxTokens *int
	Terminate bool
}

// JSONExecRequest is the validated choice between starting code and resuming a cell.
type JSONExecRequest struct {
	Source *ExecSource
	Wait   *WaitArguments
}

// jsonExecInput is shared by the exec parameter schema and the JSON decoder. Explicit null is
// rejected separately from omission so reflection and decoding keep the same concrete fields.
type jsonExecInput struct {
	Code            *string `json:"code,omitempty"`
	CellID          *string `json:"cell_id,omitempty"`
	YieldTimeMS     *uint64 `json:"yield_time_ms,omitempty" jsonschema:"minimum=0"`
	MaxOutputTokens *uint64 `json:"max_output_tokens,omitempty" jsonschema:"minimum=0"`
	Terminate       *bool   `json:"terminate,omitempty"`
}

// waitInput matches Codex rust-v0.162.1 core/src/tools/code_mode/wait_handler.rs ExecWaitArgs.
type waitInput struct {
	CellID      string  `json:"cell_id"`
	YieldTimeMS *uint64 `json:"yield_time_ms,omitempty"`
	MaxTokens   *uint64 `json:"max_tokens,omitempty"`
	Terminate   bool    `json:"terminate,omitempty"`
}

// execPragma matches Codex rust-v0.162.1 code-mode-protocol/src/description.rs CodeModeExecPragma.
type execPragma struct {
	YieldTimeMS     *uint64 `json:"yield_time_ms,omitempty"`
	MaxOutputTokens *uint64 `json:"max_output_tokens,omitempty"`
}

// ParseExecSource parses raw JavaScript and the optional Codex exec pragma.
func ParseExecSource(input string) (ExecSource, error) {
	const supported = "supported fields `yield_time_ms` and `max_output_tokens`"
	if strings.TrimSpace(input) == "" {
		return ExecSource{}, errors.New("exec expects raw JavaScript source text (non-empty). Provide JS only, optionally with first-line `// @exec: {\"yield_time_ms\": 10000, \"max_output_tokens\": 1000}`.")
	}
	firstLine, rest, _ := strings.Cut(input, "\n")
	pragma, found := strings.CutPrefix(strings.TrimLeftFunc(firstLine, unicode.IsSpace), execPragmaPrefix)
	if !found {
		return ExecSource{Code: input, YieldTimeMS: defaultExecYieldTimeMS}, nil
	}
	if strings.TrimSpace(rest) == "" {
		return ExecSource{}, errors.New("exec pragma must be followed by JavaScript source on subsequent lines")
	}
	directive := strings.TrimSpace(pragma)
	if directive == "" {
		return ExecSource{}, errors.New("exec pragma must be a JSON object with " + supported)
	}
	var value any
	if err := json.Unmarshal([]byte(directive), &value); err != nil {
		return ExecSource{}, fmt.Errorf("exec pragma must be valid JSON with %s: %w", supported, err)
	}
	fields, ok := value.(map[string]any)
	if !ok {
		return ExecSource{}, errors.New("exec pragma must be a JSON object with " + supported)
	}
	for key := range fields {
		if key != "yield_time_ms" && key != "max_output_tokens" {
			return ExecSource{}, fmt.Errorf("exec pragma only supports `yield_time_ms` and `max_output_tokens`; got `%s`", key)
		}
	}
	var arguments execPragma
	if err := json.Unmarshal([]byte(directive), &arguments); err != nil {
		return ExecSource{}, fmt.Errorf("exec pragma fields `yield_time_ms` and `max_output_tokens` must be non-negative safe integers: %w", err)
	}
	yieldMS, err := safeIntegerArgument("exec pragma field `yield_time_ms`", arguments.YieldTimeMS)
	if err != nil {
		return ExecSource{}, err
	}
	maxOutputTokens, err := safeIntegerArgument("exec pragma field `max_output_tokens`", arguments.MaxOutputTokens)
	if err != nil {
		return ExecSource{}, err
	}
	yieldTimeMS := defaultExecYieldTimeMS
	if yieldMS != nil {
		yieldTimeMS = *yieldMS
	}
	return ExecSource{Code: rest, YieldTimeMS: yieldTimeMS, MaxOutputTokens: maxOutputTokens}, nil
}

// ParseWaitArguments decodes a JSON wait call and applies Codex's omitted-yield default.
func ParseWaitArguments(input string) (WaitArguments, error) {
	var arguments waitInput
	if err := decodeToolInput([]byte(input), &arguments); err != nil {
		return WaitArguments{}, err
	}
	return resolveWaitArguments(arguments)
}

// ParseJSONExecInput validates JSON exec's code-or-cell choice and its options.
func ParseJSONExecInput(input string) (JSONExecRequest, error) {
	var arguments jsonExecInput
	if err := decodeToolInput([]byte(input), &arguments); err != nil {
		return JSONExecRequest{}, err
	}
	if (arguments.Code == nil) == (arguments.CellID == nil) {
		return JSONExecRequest{}, errors.New("exec requires exactly one of code or cell_id")
	}
	if arguments.CellID != nil {
		terminate := arguments.Terminate != nil && *arguments.Terminate
		wait, err := resolveWaitArguments(waitInput{
			CellID: *arguments.CellID, YieldTimeMS: arguments.YieldTimeMS,
			MaxTokens: arguments.MaxOutputTokens, Terminate: terminate,
		})
		if err != nil {
			return JSONExecRequest{}, err
		}
		return JSONExecRequest{Wait: &wait}, nil
	}
	if arguments.Terminate != nil {
		return JSONExecRequest{}, errors.New("terminate requires cell_id")
	}
	if strings.TrimSpace(*arguments.Code) == "" {
		return JSONExecRequest{}, errors.New("exec requires non-empty code")
	}
	yieldMS, err := safeIntegerArgument("yield_time_ms", arguments.YieldTimeMS)
	if err != nil {
		return JSONExecRequest{}, err
	}
	maxTokens, err := safeIntegerArgument("max_output_tokens", arguments.MaxOutputTokens)
	if err != nil {
		return JSONExecRequest{}, err
	}
	yieldTimeMS := defaultExecYieldTimeMS
	if yieldMS != nil {
		yieldTimeMS = *yieldMS
	}
	source := ExecSource{Code: *arguments.Code, YieldTimeMS: yieldTimeMS, MaxOutputTokens: maxTokens}
	return JSONExecRequest{Source: &source}, nil
}

func resolveWaitArguments(arguments waitInput) (WaitArguments, error) {
	if arguments.CellID == "" {
		return WaitArguments{}, errors.New("wait requires a string cell_id")
	}
	yieldMS, err := safeIntegerArgument("yield_time_ms", arguments.YieldTimeMS)
	if err != nil {
		return WaitArguments{}, err
	}
	maxTokens, err := safeIntegerArgument("max_tokens", arguments.MaxTokens)
	if err != nil {
		return WaitArguments{}, err
	}
	parsed := WaitArguments{CellID: arguments.CellID, YieldTimeMS: defaultWaitYieldTimeMS, MaxTokens: maxTokens, Terminate: arguments.Terminate}
	if yieldMS != nil {
		parsed.YieldTimeMS = *yieldMS
	}
	return parsed, nil
}

// safeIntegerArgument bounds an unsigned tool argument to the JavaScript safe integer range.
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

// Tools have a closed schema. Explicit null is invalid for every supported input field.
func decodeToolInput(data []byte, value any) error {
	var fields map[string]json.RawMessage
	if err := protocol.DecodeObject(data, &fields); err != nil {
		return err
	}
	for field, raw := range fields {
		if bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
			return fmt.Errorf("tool field %s must not be null", field)
		}
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	return decoder.Decode(value)
}
