package centralsampler

import (
	"encoding/json"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/codemode"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
)

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
	// Codex rust-v0.162.1 code-mode-protocol/src/host/payload.rs WireRuntimeResponse timing.
	CodeModeHostDurationNS *uint64 `json:"code_mode_host_duration_ns"`
}

type codeObservation struct {
	Kind codeObservationKind
	Cell codeCellOutput
}

func (o *codeObservation) UnmarshalJSON(data []byte) error {
	var variants map[string]json.RawMessage
	if err := protocol.DecodeObject(data, &variants); err != nil {
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
		if err := protocol.DecodeObject(raw, &o.Cell); err != nil {
			return err
		}
	}
	if o.Cell.CellID == "" || o.Cell.ContentItems == nil {
		return errors.New("code-mode observation requires cell_id and content_items")
	}
	return nil
}

func decodeWaitObservation(raw json.RawMessage) (codeObservation, error) {
	var reply codemode.HostResponse
	if err := protocol.DecodeObject(raw, &reply); err != nil {
		return codeObservation{}, err
	}
	if reply.Type != "wait/completed" {
		return codeObservation{}, fmt.Errorf("unexpected code-mode wait response %q", reply.Type)
	}
	var outcome map[string]json.RawMessage
	if err := protocol.DecodeObject(reply.Outcome, &outcome); err != nil {
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
		if err := protocol.DecodeObject(value, &observation); err != nil {
			return codeObservation{}, err
		}
		return observation, nil
	}
	panic("unreachable")
}
