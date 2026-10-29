package centralsampler

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
)

// Codex rust-v0.162.1 core/src/tools/code_mode/wait_handler.rs ExecWaitArgs.
type waitInput struct {
	CellID      string  `json:"cell_id"`
	YieldTimeMS *uint64 `json:"yield_time_ms,omitempty"`
	MaxTokens   *uint64 `json:"max_tokens,omitempty"`
	Terminate   bool    `json:"terminate,omitempty"`
}

// Codex rust-v0.162.1 code-mode-protocol/src/description.rs CodeModeExecPragma.
type execPragma struct {
	YieldTimeMS     *uint64 `json:"yield_time_ms,omitempty"`
	MaxOutputTokens *uint64 `json:"max_output_tokens,omitempty"`
}

// Tools have a closed schema. Explicit null is invalid for every supported input field.
func decodeToolInput(data []byte, value any) error {
	var fields map[string]json.RawMessage
	if err := decodeProtocolObject(data, &fields); err != nil {
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

func toolYieldTime(value *int, fallback int) (int, error) {
	if value == nil {
		return fallback, nil
	}
	if *value < 0 || *value > 60000 {
		return 0, errors.New("yield_time_ms must be an integer between 0 and 60000")
	}
	return *value, nil
}
