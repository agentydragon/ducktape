package tooloutput

import (
	"encoding/json"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
)

// ObservationKind identifies one code-mode runtime result variant.
type ObservationKind string

const (
	ObservationYielded    ObservationKind = "Yielded"
	ObservationTerminated ObservationKind = "Terminated"
	ObservationResult     ObservationKind = "Result"
)

// CellResult is the decoded result of one code-mode cell observation.
type CellResult struct {
	CellID       string            `json:"cell_id"`
	ContentItems []json.RawMessage `json:"content_items"`
	ErrorText    *string           `json:"error_text"`
	// Codex rust-v0.162.1 code-mode-protocol/src/host/payload.rs WireRuntimeResponse timing.
	CodeModeHostDurationNS *uint64 `json:"code_mode_host_duration_ns"`
}

// Observation combines the code-mode result variant with its concrete cell data.
type Observation struct {
	Kind ObservationKind
	Cell CellResult
}

func (o *Observation) UnmarshalJSON(data []byte) error {
	var variants map[string]json.RawMessage
	if err := protocol.DecodeObject(data, &variants); err != nil {
		return err
	}
	if len(variants) != 1 {
		return errors.New("code-mode observation must have exactly one variant")
	}
	for kind, raw := range variants {
		o.Kind = ObservationKind(kind)
		switch o.Kind {
		case ObservationYielded, ObservationTerminated, ObservationResult:
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

// DecodeWaitObservation decodes the pinned host response for a completed wait operation.
func DecodeWaitObservation(raw json.RawMessage) (Observation, error) {
	var reply struct {
		Type    string          `json:"type"`
		Outcome json.RawMessage `json:"outcome"`
	}
	if err := protocol.DecodeObject(raw, &reply); err != nil {
		return Observation{}, err
	}
	if reply.Type != "wait/completed" {
		return Observation{}, fmt.Errorf("unexpected code-mode wait response %q", reply.Type)
	}
	var outcome map[string]json.RawMessage
	if err := protocol.DecodeObject(reply.Outcome, &outcome); err != nil {
		return Observation{}, err
	}
	if len(outcome) != 1 {
		return Observation{}, errors.New("code-mode wait requires exactly one outcome")
	}
	for kind, value := range outcome {
		if kind != "LiveCell" && kind != "MissingCell" {
			return Observation{}, fmt.Errorf("unsupported code-mode wait outcome %q", kind)
		}
		var observation Observation
		if err := protocol.DecodeObject(value, &observation); err != nil {
			return Observation{}, err
		}
		return observation, nil
	}
	panic("unreachable")
}
