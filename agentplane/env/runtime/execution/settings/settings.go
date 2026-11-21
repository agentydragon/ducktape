// Package settings parses native executor limit overrides shared by the runtime and server.
package settings

import (
	"encoding/json"
	"errors"
	"io"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexshim"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/native"
)

type Limits struct {
	Native native.Limits    `json:"native"`
	Codex  codexshim.Limits `json:"codex"`
}

// Parse merges an optional JSON object into defaults and validates every limit.
func Parse(raw string) (Limits, error) {
	limits := Limits{Native: native.DefaultLimits(), Codex: codexshim.DefaultLimits()}
	if raw != "" {
		if len(raw) > 64<<10 {
			return Limits{}, errors.New("execution limits exceed 64 KiB")
		}
		if !strings.HasPrefix(strings.TrimSpace(raw), "{") {
			return Limits{}, errors.New("limits must be a JSON object")
		}
		decoder := json.NewDecoder(strings.NewReader(raw))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&limits); err != nil {
			return Limits{}, err
		}
		if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
			if err == nil {
				return Limits{}, errors.New("limits must contain one JSON object")
			}
			return Limits{}, err
		}
	}
	if err := limits.Native.Validate(); err != nil {
		return Limits{}, err
	}
	if err := limits.Codex.Validate(); err != nil {
		return Limits{}, err
	}
	return limits, nil
}
